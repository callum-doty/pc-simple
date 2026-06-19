# FIX-002: LLM Cost Governance + Retry Checkpointing

**Priority:** P0  
**Effort:** ~4 hours  
**Files affected:** `models/document.py`, `worker.py`, `services/ai_service.py`, one new Alembic migration

---

## Problem

### No per-document cost tracking

Every call to Claude, OpenAI, or Gemini in `ai_service.py` discards the token usage
data returned by the API. There is no field on `Document` for token counts or estimated
cost. There is no way to know what any document cost to process, which providers were
used, or how cost evolves as the AI pipeline changes.

At current single-operator scale this is a monitoring gap. At 10x document volume it
becomes a budget control gap — you cannot enforce limits or route by cost without this
data.

### Retry cost amplification

`process_document_task` (worker.py:183) has no Celery retry configuration. When it
fails — network error, LLM timeout, rate limit — Celery's default behavior is to not
retry. However, the scheduled `enqueue_documents_task` (runs every 2 minutes) will
re-dispatch the document, restarting from page 1.

For a 40-page PDF that fails on page 35, restarting means 35 pages of LLM work are
re-done. At 3 failures before success, that is 3 × 35 + 40 = 145 pages billed instead
of 40. Under a rate limit storm or persistent transient errors, this multiplier grows
unbounded.

`extract_document_features_task` (worker.py:280) does have `max_retries=3` and is
largely idempotent, so that path is handled.

### No spend cap

There is no mechanism to pause ingestion if LLM spend exceeds a threshold. A misconfigured
Dropbox ingest batch + high retry rate is a silent cost event.

---

## Solution

### Part A — Cost tracking in Document

Add token/cost fields to `documents` table (stored as integers to avoid float precision):
- `llm_tokens_input` — cumulative input tokens across all LLM calls for this document
- `llm_tokens_output` — cumulative output tokens
- `llm_provider_used` — which provider processed this document

These are stored in `file_metadata` JSONB under a `processing_cost` key — no schema
migration needed for the initial version. Migrate to dedicated columns if cost queries
become frequent.

### Part B — Checkpoint-aware processing

Track the last successfully completed page in `file_metadata.processing_checkpoint`.
On retry, resume from the checkpoint page rather than page 1.

### Part C — Provider-level cost accumulator in AIService

Return token usage from all LLM call sites and accumulate it on the document record
at the end of each processing task.

---

## Implementation Steps

### Step 1 — Return token usage from `ai_service.py`

Each LLM provider returns usage data. Capture it.

**Anthropic** (`anthropic_client.messages.create` response):
```python
response.usage.input_tokens
response.usage.output_tokens
```

**OpenAI** (`openai_client.chat.completions.create` response):
```python
response.usage.prompt_tokens
response.usage.completion_tokens
```

**Gemini** (`model.generate_content` response):
```python
response.usage_metadata.prompt_token_count
response.usage_metadata.candidates_token_count
```

In `ai_service.py`, modify `analyze_text_chunk_sync` (and any other method that calls
an LLM) to return a tuple of `(result, usage_dict)`:

```python
def analyze_text_chunk_sync(
    self, text: str, filename: str, analysis_type: str
) -> tuple[dict, dict]:
    """Returns (analysis_result, usage) where usage = {input_tokens, output_tokens, provider}"""
    # ... existing logic ...

    if self.ai_provider == "anthropic":
        response = self.anthropic_client.messages.create(...)
        usage = {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
            "provider": "anthropic",
        }
        return parse_result(response), usage

    elif self.ai_provider == "openai":
        response = self.openai_client.chat.completions.create(...)
        usage = {
            "input_tokens": response.usage.prompt_tokens,
            "output_tokens": response.usage.completion_tokens,
            "provider": "openai",
        }
        return parse_result(response), usage
    # ... gemini similarly ...
```

---

### Step 2 — Accumulate usage in `worker.py`

In `_process_pdf_document_by_page` (worker.py:40), accumulate across pages:

```python
total_input_tokens = 0
total_output_tokens = 0
provider_used = None

for page_num, page_text in text_generator:
    chunk_analysis, usage = ai_service.analyze_text_chunk_sync(
        page_text, document.filename, analysis_type
    )
    total_input_tokens += usage.get("input_tokens", 0)
    total_output_tokens += usage.get("output_tokens", 0)
    provider_used = provider_used or usage.get("provider")
    # ... existing aggregation ...
```

At the end of processing, store in `file_metadata`:

```python
document_service.update_document_content_sync(
    document_id,
    # ... existing args ...
)

# Append cost data to file_metadata (non-destructive merge)
cost_meta = {
    "processing_cost": {
        "input_tokens": total_input_tokens,
        "output_tokens": total_output_tokens,
        "provider": provider_used,
        "processed_at": datetime.utcnow().isoformat(),
    }
}
document_service.update_document_metadata_sync(document_id, cost_meta)
```

---

### Step 3 — Checkpoint-aware PDF retry

In `_process_pdf_document_by_page`, persist a checkpoint after each successfully
processed page:

```python
for page_num, page_text in text_generator:
    # Skip pages already processed in a previous attempt
    checkpoint = document_service.get_document_metadata_sync(
        document_id, "processing_checkpoint", default=0
    )
    if page_num <= checkpoint:
        logger.info(f"Skipping page {page_num} (already checkpointed)")
        continue

    chunk_analysis, usage = ai_service.analyze_text_chunk_sync(...)
    # ... aggregate ...

    # Write checkpoint after each successful page
    document_service.update_document_metadata_sync(
        document_id, {"processing_checkpoint": page_num}
    )
```

Clear the checkpoint on task completion and on explicit reprocess:

```python
# In process_document_task finally block (after COMPLETED status)
document_service.update_document_metadata_sync(
    document_id, {"processing_checkpoint": None}
)
```

In `services/document_service.py`, add `reset_document_for_reprocessing` to clear
the checkpoint alongside other reprocess fields.

---

### Step 4 — Add retry configuration to `process_document_task`

Add Celery retry parameters with exponential backoff to `process_document_task`:

```python
@celery_app.task(
    name="process_document_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,    # 1 minute base delay
)
def process_document_task(self, document_id: int, analysis_type: str = "unified"):
    # ...
    except anthropic.RateLimitError as e:
        logger.warning(f"Rate limit hit for document {document_id}, retrying...")
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))
    except openai.RateLimitError as e:
        raise self.retry(exc=e, countdown=60 * (2 ** self.request.retries))
    except Exception as e:
        if self.request.retries < self.max_retries:
            raise self.retry(exc=e, countdown=30)
        # Max retries exceeded — mark as FAILED
        document_service.update_document_status_sync(
            document_id, DocumentStatus.FAILED, progress=0, error=str(e)
        )
```

With checkpointing in place, retries resume from the last completed page instead of
restarting from page 1. This caps the cost multiplier at approximately:
`(pages_per_retry_attempt × number_of_retries) + total_pages`
instead of `total_pages × number_of_retries`.

---

### Step 5 — Expose cost in `/api/stats` and document detail

In `main.py` at line 1435 (`/api/stats`), add aggregate token counts:

```python
# Add to the stats query
total_tokens = db.query(
    func.sum(
        func.cast(
            Document.file_metadata["processing_cost"]["input_tokens"].astext,
            Integer
        )
    )
).scalar() or 0
```

In `Document.to_dict()` (`models/document.py:325`), include cost in `full_detail`:

```python
if full_detail:
    data.update({
        # ... existing fields ...
        "processing_cost": self.get_metadata("processing_cost"),
    })
```

---

## Acceptance Criteria

- [ ] After a document is processed, `document.file_metadata["processing_cost"]` contains
      `input_tokens`, `output_tokens`, and `provider`.
- [ ] A 10-page PDF that fails on page 8 and is retried starts processing from page 8,
      not page 1.
- [ ] Rate limit errors from Anthropic or OpenAI trigger a Celery retry with exponential
      backoff rather than immediately marking the document FAILED.
- [ ] `/api/stats` includes aggregate token counts.
- [ ] The existing behavior of non-PDF documents (holistic processing) is unchanged.

---

## Notes

**Cost estimation.** Actual dollar cost is not stored because pricing changes over time.
Token counts are the durable unit. Cost can be calculated on-read from token counts
and the provider's current pricing.

**Checkpoint durability.** The checkpoint is stored in `file_metadata` (JSONB), which
is committed to PostgreSQL after each page. If the process crashes before the commit,
the checkpoint for that page is lost, but the worst case is re-processing one page —
not the entire document.

**Spend cap.** A simple implementation: query aggregate tokens in the last 24 hours
before dispatching new tasks in `enqueue_pending_documents`. If above a threshold,
log a warning and pause dispatch. This is left as a follow-on task.
