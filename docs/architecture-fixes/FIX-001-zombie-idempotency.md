# FIX-001: Zombie Task Recovery + Processing Lock

**Priority:** P0  
**Effort:** ~3 hours  
**Files affected:** `models/document.py`, `worker.py`, `services/scheduler_service.py`, one new Alembic migration

---

## Problem

### Zombie tasks

`process_document_task` sets status to `PROCESSING` at `worker.py:206` and never resets it
on worker crash or container restart. The recovery scheduler (`enqueue_documents_task`)
at `worker.py:162` only queries for `QUEUED` documents:

```python
# services/scheduler_service.py (inferred from worker.py:174)
scheduler_service.enqueue_pending_documents()
```

Any document in `PROCESSING` when the worker is restarted — which happens on every
Render deploy — stays in `PROCESSING` forever with no alert and no recovery.
The processing pipeline silently stalls.

### Duplicate processing (idempotency gap)

The proposed fix for zombie tasks (reset PROCESSING → QUEUED after timeout) creates a
second problem: if the original worker is still alive but slow (e.g., waiting on an LLM
API response), resetting status to QUEUED causes a second worker to pick up and process
the same document concurrently.

The result is two workers calling the LLM APIs for the same document simultaneously,
with a write race on `ai_analysis`, `keywords`, and `search_vector`. The last writer
wins — potentially with a different result than the first.

The two bugs must be fixed together. Zombie recovery is only safe when combined with
an exclusive processing lock.

---

## Solution

### Part A — Heartbeat timestamp (DB schema)

Add `processing_heartbeat_at` to the `documents` table. The worker updates this field
every N seconds during long operations. A zombie is defined as: `status = PROCESSING`
AND `processing_heartbeat_at < NOW() - (task_timeout + grace_period)`.

### Part B — Redis distributed lock (idempotency)

Acquire a `SET NX` lock in Redis at the start of `process_document_task`. If the lock
is already held, the task exits without processing. This prevents two workers from ever
processing the same document simultaneously.

### Part C — Recovery scheduler

Extend `enqueue_documents_task` to find zombie PROCESSING documents (heartbeat expired)
and reset them to QUEUED after first releasing any stale lock.

---

## Implementation Steps

### Step 1 — Add `processing_heartbeat_at` column

In `models/document.py`, add after `processing_started_at` (line 53):

```python
processing_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
```

Generate a new Alembic migration:

```bash
alembic revision --autogenerate -m "add processing_heartbeat_at"
```

Verify the generated migration adds `processing_heartbeat_at` as a nullable DateTime
with no server default. Apply: `alembic upgrade head`.

---

### Step 2 — Acquire lock at task start in `worker.py`

Add a lock helper before `process_document_task`:

```python
import redis as redis_lib
from datetime import datetime, timezone

TASK_TIMEOUT_SECONDS = 300      # must match celery task_time_limit
HEARTBEAT_INTERVAL_SECONDS = 30
LOCK_TTL_SECONDS = TASK_TIMEOUT_SECONDS + 60  # grace period

def _acquire_processing_lock(document_id: int) -> tuple[bool, object | None]:
    """
    Attempt to acquire an exclusive processing lock for a document.
    Returns (acquired: bool, redis_client | None).
    Uses SET NX with TTL so locks self-expire if the worker dies without releasing.
    """
    try:
        r = redis_lib.from_url(settings.redis_url, decode_responses=True)
        lock_key = f"doc_processing_lock:{document_id}"
        acquired = r.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
        return bool(acquired), r
    except Exception as e:
        logger.warning(f"Could not acquire Redis lock for document {document_id}: {e}")
        # If Redis is unavailable, allow processing to continue (degraded mode)
        return True, None


def _release_processing_lock(document_id: int, redis_client) -> None:
    if redis_client is None:
        return
    try:
        redis_client.delete(f"doc_processing_lock:{document_id}")
    except Exception as e:
        logger.warning(f"Could not release Redis lock for document {document_id}: {e}")
```

---

### Step 3 — Update `process_document_task` to use the lock and emit heartbeats

Modify `process_document_task` in `worker.py` starting at line 183:

```python
@celery_app.task(name="process_document_task")
def process_document_task(document_id: int, analysis_type: str = "unified"):
    db = None
    redis_client = None
    try:
        # --- Idempotency guard ---
        acquired, redis_client = _acquire_processing_lock(document_id)
        if not acquired:
            logger.info(
                f"Document {document_id} is already being processed by another worker. Skipping."
            )
            return False

        db = next(get_db())
        document_service = DocumentService(db)
        # ... rest of setup ...

        document_service.update_document_status_sync(
            document_id, DocumentStatus.PROCESSING, progress=10
        )
        # Record heartbeat at transition to PROCESSING
        _emit_heartbeat(document_id, db)

        # Pass redis_client and document_id into sub-functions so they can
        # emit periodic heartbeats during long PDF/LLM operations.
        # ...

    except Exception as e:
        # ... existing error handling ...
    finally:
        _release_processing_lock(document_id, redis_client)
        if db:
            db.close()
```

Add a heartbeat helper that sub-functions can call periodically (e.g., once per PDF page):

```python
def _emit_heartbeat(document_id: int, db) -> None:
    """Update processing_heartbeat_at to signal the worker is still alive."""
    try:
        from models.document import Document
        from sqlalchemy import update
        db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(processing_heartbeat_at=datetime.now(timezone.utc))
        )
        db.commit()
    except Exception as e:
        logger.warning(f"Heartbeat update failed for document {document_id}: {e}")
```

Call `_emit_heartbeat` inside the page loop in `_process_pdf_document_by_page`
(currently at `worker.py:61`) once per page.

---

### Step 4 — Extend the recovery scheduler

In `services/scheduler_service.py`, add zombie detection to `enqueue_pending_documents`:

```python
from datetime import datetime, timezone, timedelta

ZOMBIE_THRESHOLD_SECONDS = 360  # task timeout (300) + 60s grace

def enqueue_pending_documents(self) -> None:
    # --- Existing: enqueue QUEUED documents ---
    queued_docs = (
        self.db.query(Document)
        .filter(Document.status == DocumentStatus.QUEUED)
        .all()
    )
    for doc in queued_docs:
        process_document_task.delay(doc.id)

    # --- New: rescue zombie PROCESSING documents ---
    zombie_cutoff = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS)
    zombie_docs = (
        self.db.query(Document)
        .filter(
            Document.status == DocumentStatus.PROCESSING,
            Document.processing_heartbeat_at < zombie_cutoff,
        )
        .all()
    )
    for doc in zombie_docs:
        logger.warning(
            f"Document {doc.id} stuck in PROCESSING since {doc.processing_heartbeat_at}. "
            f"Resetting to QUEUED."
        )
        # Release any stale Redis lock before re-queuing
        _release_processing_lock(doc.id, redis_client=None)  # best-effort
        doc.status = DocumentStatus.QUEUED
        doc.processing_heartbeat_at = None
        doc.processing_error = "Reset from zombie PROCESSING state by scheduler"
    if zombie_docs:
        self.db.commit()
        logger.info(f"Rescued {len(zombie_docs)} zombie document(s).")
```

Note: `processing_heartbeat_at IS NULL` documents in PROCESSING are also zombies
(pre-migration documents). Add a secondary filter for those:

```python
from sqlalchemy import or_

.filter(
    Document.status == DocumentStatus.PROCESSING,
    or_(
        Document.processing_heartbeat_at < zombie_cutoff,
        Document.processing_heartbeat_at.is_(None),
    ),
)
```

Documents with a NULL heartbeat and PROCESSING status are pre-migration zombies.
The NULL case should be treated as a zombie only after `processing_started_at` has
passed the threshold. Adjust the query accordingly using `processing_started_at` as
the fallback timestamp.

---

## Acceptance Criteria

- [ ] A document that is PROCESSING when the Celery worker is restarted returns to
      QUEUED within the next scheduler run (≤ 2 minutes + zombie threshold).
- [ ] Starting `process_document_task` for the same document_id twice concurrently
      results in exactly one processing run; the second logs "already being processed"
      and exits without calling any LLM API.
- [ ] `processing_heartbeat_at` is updated at least once per page during PDF processing.
- [ ] A document whose worker dies mid-page does not remain in PROCESSING indefinitely.
- [ ] The existing processing flow for a single document is unchanged.

---

## Notes

The Redis lock TTL (`LOCK_TTL_SECONDS = 360`) must be longer than the maximum possible
task duration. If you change `task_time_limit` in Celery config, update `LOCK_TTL_SECONDS`
to match.

If Redis is unavailable, `_acquire_processing_lock` returns `(True, None)` to allow
processing to continue in degraded mode. The zombie recovery scheduler will still work
via `processing_heartbeat_at` in this case.
