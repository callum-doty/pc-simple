"""
Tests for the zombie-recovery, heartbeat, and checkpoint-resume logic added in
FIX-001 (docs/architecture-fixes/FIX-001-zombie-idempotency.md) and FIX-002
(docs/architecture-fixes/FIX-002-llm-cost-governance.md).

These are logic-level unit tests against a mocked SQLAlchemy Session. The
Document model uses Postgres-only column types (JSONB, TSVECTOR via a raw
`to_tsvector` Computed column, pgvector's Vector) so it cannot be created
against SQLite, and no Postgres instance is available in this environment.
Mocking the Session lets us exercise the real service/worker code paths
(state transitions, commit/rollback, statement construction) without a live
database. It does not verify the SQL predicate inside `_rescue_zombie_documents`
against real data — that would require an integration test against Postgres.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, Mock, call

import pytest

from models.document import DocumentStatus
from services.scheduler_service import SchedulerService, ZOMBIE_THRESHOLD_SECONDS


def make_fake_document(**overrides):
    doc = Mock()
    doc.id = overrides.get("id", 1)
    doc.status = overrides.get("status", DocumentStatus.PROCESSING)
    doc.processing_heartbeat_at = overrides.get("processing_heartbeat_at")
    doc.processing_started_at = overrides.get("processing_started_at")
    doc.updated_at = overrides.get("updated_at")
    doc.processing_error = None
    doc.created_at = overrides.get("created_at", datetime.now(timezone.utc))
    return doc


@pytest.fixture
def fake_dispatch(monkeypatch):
    """
    Patch the Celery dispatch. The recovery sweeps redispatch what they rescue
    (Option A: the daemon recovers *and* requeues, because nothing else drains
    QUEUED any more), so without this they would reach for a real broker.
    """
    import worker

    task = Mock()
    monkeypatch.setattr(worker, "process_document_task", task)
    return task


class TestRescueZombieDocuments:
    def test_resets_stale_heartbeat_zombie(self, fake_dispatch):
        stale_heartbeat = datetime.now(timezone.utc) - timedelta(
            seconds=ZOMBIE_THRESHOLD_SECONDS + 10
        )
        zombie = make_fake_document(
            id=1, processing_heartbeat_at=stale_heartbeat, processing_started_at=stale_heartbeat
        )

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [zombie]

        service = SchedulerService(db)
        rescued = service._rescue_zombie_documents()

        assert rescued == 1
        assert zombie.status == DocumentStatus.QUEUED
        assert zombie.processing_heartbeat_at is None
        assert "zombie" in zombie.processing_error.lower()
        db.commit.assert_called_once()
        # Recovery must requeue: nothing else drains QUEUED.
        fake_dispatch.delay.assert_called_once_with(1)

    def test_noop_when_no_zombies_found(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        service = SchedulerService(db)
        rescued = service._rescue_zombie_documents()

        assert rescued == 0
        db.commit.assert_not_called()

    def test_rescues_multiple_zombies_in_one_pass(self, fake_dispatch):
        stale = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS + 5)
        zombies = [make_fake_document(id=i, processing_heartbeat_at=stale) for i in (1, 2, 3)]

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = zombies

        service = SchedulerService(db)
        rescued = service._rescue_zombie_documents()

        assert rescued == 3
        assert all(z.status == DocumentStatus.QUEUED for z in zombies)
        db.commit.assert_called_once()


class TestRescueStrandedPendingDocuments:
    """
    PENDING is the momentary state between the scheduler claiming a document and
    dispatching it. _rescue_zombie_documents only queries PROCESSING, so before
    this rescue existed a document whose dispatch never landed was invisible to
    recovery forever.
    """

    def test_resets_stranded_pending_to_queued(self, fake_dispatch):
        stale = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS + 10)
        stranded = make_fake_document(id=7, status=DocumentStatus.PENDING, updated_at=stale)

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [stranded]

        service = SchedulerService(db)
        rescued = service._rescue_stranded_pending_documents()

        assert rescued == 1
        assert stranded.status == DocumentStatus.QUEUED
        assert "stranded" in stranded.processing_error.lower()
        db.commit.assert_called_once()
        fake_dispatch.delay.assert_called_once_with(7)

    def test_noop_when_nothing_stranded(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        service = SchedulerService(db)

        assert service._rescue_stranded_pending_documents() == 0
        db.commit.assert_not_called()


class TestRescueUndispatchedDocuments:
    """
    QUEUED documents whose dispatch never reached the broker. This sweep is
    gated on an empty broker queue: a QUEUED document is otherwise
    indistinguishable from one waiting its turn behind a bulk load, and
    redispatching those would amplify a large backlog every cycle.
    """

    def _service(self, monkeypatch, backlog, docs):
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.all.return_value = docs
        service = SchedulerService(db)
        monkeypatch.setattr(service, "_broker_backlog", lambda: backlog)
        return service

    def test_redispatches_when_broker_is_empty(self, monkeypatch, fake_dispatch):
        orphan = make_fake_document(id=6, status=DocumentStatus.QUEUED)
        service = self._service(monkeypatch, 0, [orphan])

        assert service._rescue_undispatched_documents() == 1
        fake_dispatch.delay.assert_called_once_with(6)

    def test_skips_while_a_backlog_is_still_draining(self, monkeypatch, fake_dispatch):
        orphan = make_fake_document(id=6, status=DocumentStatus.QUEUED)
        service = self._service(monkeypatch, 7000, [orphan])

        assert service._rescue_undispatched_documents() == 0
        fake_dispatch.delay.assert_not_called()

    def test_skips_when_broker_depth_is_unknown(self, monkeypatch, fake_dispatch):
        # None != 0: an unreadable broker must not be treated as an empty one.
        orphan = make_fake_document(id=6, status=DocumentStatus.QUEUED)
        service = self._service(monkeypatch, None, [orphan])

        assert service._rescue_undispatched_documents() == 0
        fake_dispatch.delay.assert_not_called()


class TestRunRecoveryCycle:
    def test_runs_all_three_sweeps_and_reports_counts(self):
        db = MagicMock()
        service = SchedulerService(db)
        service._rescue_zombie_documents = Mock(return_value=2)
        service._rescue_stranded_pending_documents = Mock(return_value=1)
        service._rescue_undispatched_documents = Mock(return_value=6)

        assert service.run_recovery_cycle() == {
            "zombie": 2,
            "pending": 1,
            "undispatched": 6,
        }

    def test_a_failing_sweep_rolls_back_and_does_not_raise(self):
        db = MagicMock()
        service = SchedulerService(db)
        service._rescue_zombie_documents = Mock(side_effect=RuntimeError("db is down"))

        # Beat must keep firing even if one cycle fails.
        counts = service.run_recovery_cycle()

        assert counts == {"zombie": 0, "pending": 0, "undispatched": 0}
        db.rollback.assert_called_once()

    def test_dispatch_failure_leaves_the_document_queued(self, monkeypatch):
        # A broker outage must not strand a document: it stays QUEUED so a later
        # cycle's undispatched sweep picks it up.
        import worker

        task = Mock()
        task.delay.side_effect = ConnectionError("broker is down")
        monkeypatch.setattr(worker, "process_document_task", task)

        doc = make_fake_document(id=9, status=DocumentStatus.QUEUED)
        service = SchedulerService(MagicMock())

        service._dispatch([doc])  # must not raise

        assert doc.status == DocumentStatus.QUEUED


class TestEmitHeartbeat:
    def test_updates_heartbeat_for_correct_document_and_commits(self):
        import worker

        db = MagicMock()
        worker._emit_heartbeat(document_id=42, db=db)

        db.execute.assert_called_once()
        stmt = db.execute.call_args[0][0]
        compiled = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        assert "documents" in compiled
        assert "processing_heartbeat_at" in compiled
        assert "42" in compiled
        db.commit.assert_called_once()

    def test_swallows_errors_without_raising(self):
        import worker

        db = MagicMock()
        db.execute.side_effect = RuntimeError("db is down")

        # Must not raise — heartbeat failures are non-fatal (best-effort signal).
        worker._emit_heartbeat(document_id=1, db=db)
        db.commit.assert_not_called()


class TestPdfCheckpointResume:
    def test_resumes_from_checkpoint_and_skips_already_processed_pages(self, monkeypatch):
        import worker

        document = Mock()
        document.filename = "test.pdf"
        document.file_path = "/fake/test.pdf"
        file_metadata = Mock()
        file_metadata.processing_checkpoint = 2  # retry resuming after page 2
        document.get_file_metadata.return_value = file_metadata

        ai_service = Mock()
        ai_service.extract_text_from_pdf_sync_generator.return_value = iter(
            [(1, "page one text"), (2, "page two text"), (3, "page three text")]
        )
        ai_service.analyze_text_chunk_sync.return_value = {"summary": "page 3 summary"}
        ai_service._extract_keywords_from_analysis.return_value = ([], [])
        ai_service._extract_mappings_from_analysis.return_value = []
        ai_service.ai_provider = "anthropic"

        storage_service = Mock()
        storage_service.get_file_sync.return_value = b"fake-pdf-bytes"

        document_service = Mock()

        db = MagicMock()
        checkpoint_doc = Mock()
        db.get.return_value = checkpoint_doc

        heartbeat_calls = []
        monkeypatch.setattr(
            worker, "_emit_heartbeat", lambda doc_id, db, lease=None: heartbeat_calls.append(doc_id)
        )

        worker._process_pdf_document_by_page(
            document_id=99,
            document=document,
            document_service=document_service,
            ai_service=ai_service,
            storage_service=storage_service,
            analysis_type="unified",
            db=db,
        )

        # Only page 3 should have been analyzed — pages 1-2 are already checkpointed.
        assert ai_service.analyze_text_chunk_sync.call_count == 1
        analyzed_page_text = ai_service.analyze_text_chunk_sync.call_args[0][0]
        assert analyzed_page_text == "page three text"

        # Heartbeat should only fire for the page actually processed.
        assert heartbeat_calls == [99]

        # Checkpoint should advance to the last processed page.
        checkpoint_doc.set_metadata.assert_called_once_with(processing_checkpoint=3)

        # Final persisted text/analysis should not include the skipped pages.
        _, kwargs = document_service.update_document_content_sync.call_args
        assert "page one text" not in kwargs["extracted_text"]
        assert "page two text" not in kwargs["extracted_text"]
        assert "page three text" in kwargs["extracted_text"]


class FakeRedis:
    """Minimal in-memory stand-in for the redis client calls the lease makes."""

    def __init__(self):
        self.store = {}
        self.ttls = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        self.ttls[key] = ex
        return True

    def get(self, key):
        return self.store.get(key)

    def expire(self, key, ttl):
        if key in self.store:
            self.ttls[key] = ttl
            return True
        return False

    def delete(self, key):
        self.store.pop(key, None)
        self.ttls.pop(key, None)


class TestProcessingLease:
    """
    The lease is what makes acks_late safe. With acks_late enabled a task killed
    mid-flight is redelivered under the *same* Celery task id, so it must be able
    to reclaim the lease its own dead attempt left behind — otherwise the
    redelivery is turned away by its own lock and the document is stranded in
    PROCESSING exactly as before.
    """

    def _lease(self, monkeypatch, fake):
        import worker

        monkeypatch.setattr(worker.redis, "from_url", lambda *a, **kw: fake)
        return worker

    def test_acquires_when_free(self, monkeypatch):
        worker = self._lease(monkeypatch, FakeRedis())
        acquired, lease = worker._acquire_processing_lease(1, "task-a")
        assert acquired is True
        assert lease.client.get("doc_processing_lock:1") == "task-a"

    def test_turns_away_a_different_live_worker(self, monkeypatch):
        fake = FakeRedis()
        worker = self._lease(monkeypatch, fake)
        worker._acquire_processing_lease(1, "task-a")

        acquired, _ = worker._acquire_processing_lease(1, "task-b")

        assert acquired is False
        assert fake.get("doc_processing_lock:1") == "task-a"

    def test_reclaims_own_lease_on_redelivery(self, monkeypatch):
        # Simulates: worker dies holding the lease, acks_late redelivers the
        # same task id, and the new attempt must take its own lease back.
        fake = FakeRedis()
        worker = self._lease(monkeypatch, fake)
        worker._acquire_processing_lease(1, "task-a")  # first attempt, never released

        acquired, lease = worker._acquire_processing_lease(1, "task-a")

        assert acquired is True
        assert fake.ttls["doc_processing_lock:1"] == worker.LOCK_TTL_SECONDS

    def test_release_does_not_drop_another_workers_lease(self, monkeypatch):
        fake = FakeRedis()
        worker = self._lease(monkeypatch, fake)
        _, mine = worker._acquire_processing_lease(1, "task-a")
        fake.store["doc_processing_lock:1"] = "task-b"  # taken over after expiry

        mine.release()

        assert fake.get("doc_processing_lock:1") == "task-b"

    def test_refresh_extends_only_our_own_lease(self, monkeypatch):
        fake = FakeRedis()
        worker = self._lease(monkeypatch, fake)
        _, mine = worker._acquire_processing_lease(1, "task-a")
        fake.ttls["doc_processing_lock:1"] = 5

        mine.refresh()
        assert fake.ttls["doc_processing_lock:1"] == worker.LOCK_TTL_SECONDS

        fake.store["doc_processing_lock:1"] = "task-b"
        fake.ttls["doc_processing_lock:1"] = 5
        mine.refresh()
        assert fake.ttls["doc_processing_lock:1"] == 5  # untouched

    def test_degrades_to_noop_when_redis_is_down(self, monkeypatch):
        import worker

        def boom(*a, **kw):
            raise ConnectionError("redis is down")

        monkeypatch.setattr(worker.redis, "from_url", boom)

        acquired, lease = worker._acquire_processing_lease(1, "task-a")

        # Processing must continue unguarded rather than halt; heartbeat-based
        # zombie recovery remains the backstop.
        assert acquired is True
        assert lease.client is None
        lease.refresh()
        lease.release()


class TestRescueReleasesLease:
    def test_zombie_rescue_clears_the_dead_workers_lease(self, monkeypatch, fake_dispatch):
        """
        FIX-001 Part C. Without this the rescued document is redispatched while
        the dead worker's lease is still held, the new task is turned away by
        `if not acquired`, and it strands again — the exact loop the rescue
        exists to break.
        """
        import worker

        released = []
        monkeypatch.setattr(worker, "release_processing_lease", released.append)

        stale = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS + 10)
        zombies = [
            make_fake_document(id=i, processing_heartbeat_at=stale) for i in (4, 5)
        ]

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = zombies

        service = SchedulerService(db)
        assert service._rescue_zombie_documents() == 2
        assert released == [4, 5]

    def test_release_survives_redis_being_down(self, monkeypatch):
        import worker

        def boom(*a, **kw):
            raise ConnectionError("redis is down")

        monkeypatch.setattr(worker.redis, "from_url", boom)

        # Must not raise — the lease expires on its own via TTL.
        worker.release_processing_lease(1)

    def test_release_deletes_the_lease_key(self, monkeypatch):
        import worker

        fake = FakeRedis()
        monkeypatch.setattr(worker.redis, "from_url", lambda *a, **kw: fake)
        worker._acquire_processing_lease(3, "task-a")
        assert fake.get("doc_processing_lock:3") == "task-a"

        worker.release_processing_lease(3)

        assert fake.get("doc_processing_lock:3") is None


class TestBeatSchedule:
    def test_every_scheduled_task_is_registered(self):
        """
        Beat publishes by name. An explicit name= on a task replaces the
        module-qualified default, so a schedule entry saying "worker.foo" for a
        task registered as "foo" publishes messages no worker will handle — the
        worker logs "Received unregistered task" and the work silently never
        happens. The previous schedule did exactly that, which is why simply
        starting beat would not have fixed recovery on its own.
        """
        import worker

        app = worker.celery_app
        for entry_name, entry in app.conf.beat_schedule.items():
            assert entry["task"] in app.tasks, (
                f"beat entry {entry_name!r} publishes {entry['task']!r}, "
                f"which is not a registered task"
            )


class TestRequeueClearsRunTimings:
    """
    processing_started_at is stamped with coalesce so the PDF path's per-page
    status writes cannot overwrite it mid-run. That only measures the *current*
    run if the previous run's value is cleared on requeue — otherwise a
    reprocessed document keeps its first-ever start time and both the zombie
    fallback and dashboard_service's avg(processed_at - processing_started_at)
    read months instead of minutes.
    """

    def test_zombie_rescue_clears_previous_run_timings(self, fake_dispatch):
        stale = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS + 10)
        zombie = make_fake_document(
            id=1, processing_heartbeat_at=stale, processing_started_at=stale
        )

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [zombie]

        SchedulerService(db)._rescue_zombie_documents()

        assert zombie.processing_started_at is None
        assert zombie.processing_heartbeat_at is None

    def test_pending_rescue_clears_previous_run_timings(self, fake_dispatch):
        stale = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS + 10)
        stranded = make_fake_document(
            id=7,
            status=DocumentStatus.PENDING,
            updated_at=stale,
            processing_started_at=stale,
        )

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = [stranded]

        SchedulerService(db)._rescue_stranded_pending_documents()

        assert stranded.processing_started_at is None
        assert stranded.processing_heartbeat_at is None

    def test_status_setter_clears_timings_when_requeueing(self):
        from services.document_service import DocumentService

        db = MagicMock()
        DocumentService(db).update_document_status_sync(1, DocumentStatus.QUEUED)

        stmt = db.execute.call_args[0][0]
        values = stmt.compile().params
        assert values["processing_started_at"] is None
        assert values["processing_heartbeat_at"] is None
