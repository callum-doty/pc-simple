import os
import redis
from celery import Celery
from config import get_settings
from services.document_service import DocumentService
from services.ai_service import AIService
from services.storage_service import StorageService
from services.preview_service import PreviewService
from services.feature_extraction_service import extract_document_features, load_canonical_map_from_db
from models.document import DocumentStatus
import logging
from typing import Generator, Tuple, List, Dict, Any, Optional
from collections import defaultdict
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
settings = get_settings()

# ---------------------------------------------------------------------------
# Processing lock constants — must be consistent with task timeout settings.
# LOCK_TTL_SECONDS must exceed the longest possible task duration so that a
# crashed worker's lock expires before the recovery scheduler reschedules the
# document. See docs/architecture-fixes/FIX-001.
# ---------------------------------------------------------------------------
TASK_TIMEOUT_SECONDS = 300         # matches config.settings.processing_timeout
HEARTBEAT_INTERVAL_PAGES = 1       # emit heartbeat every N PDF pages
LOCK_TTL_SECONDS = TASK_TIMEOUT_SECONDS + 60  # grace period beyond task timeout


def _acquire_processing_lock(document_id: int) -> tuple:
    """
    Attempt to acquire an exclusive processing lock for a document via Redis SET NX.
    Returns (acquired: bool, redis_client | None).

    If Redis is unavailable, returns (True, None) so processing continues in
    degraded mode — zombie recovery via processing_heartbeat_at still works.
    """
    try:
        r = redis.from_url(settings.redis_url, decode_responses=True)
        lock_key = f"doc_processing_lock:{document_id}"
        acquired = r.set(lock_key, "1", nx=True, ex=LOCK_TTL_SECONDS)
        return bool(acquired), r
    except Exception as e:
        logger.warning(
            f"Could not acquire Redis lock for document {document_id} (Redis unavailable?): {e}. "
            f"Proceeding without lock — zombie recovery via heartbeat still active."
        )
        return True, None


def _release_processing_lock(document_id: int, redis_client) -> None:
    """Release the processing lock. Safe to call even if lock was never acquired."""
    if redis_client is None:
        return
    try:
        redis_client.delete(f"doc_processing_lock:{document_id}")
    except Exception as e:
        logger.warning(f"Could not release Redis lock for document {document_id}: {e}")


def _emit_heartbeat(document_id: int, db) -> None:
    """
    Update processing_heartbeat_at to signal the worker is still alive.
    Called at task start and periodically during long PDF processing runs.
    The scheduler uses this timestamp to detect zombie PROCESSING documents.
    """
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

celery_app = Celery(
    "worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "enqueue-documents-every-two-minutes": {
            "task": "worker.enqueue_documents_task",
            "schedule": 120.0,  # 2 minutes
        },
    },
)


def _process_pdf_document_by_page(
    document_id: int,
    document,
    document_service: DocumentService,
    ai_service: AIService,
    storage_service: StorageService,
    analysis_type: str,
    db=None,
):
    """
    Process PDF documents page by page with heartbeat emission (FIX-001) and
    per-page checkpointing for cost-efficient retries (FIX-002).
    """
    logger.info(f"Processing PDF document {document_id} page by page.")
    file_content = storage_service.get_file_sync(document.file_path)
    if not file_content:
        raise ValueError(f"Could not retrieve file content for {document.filename}")

    # --- FIX-002: Resume from checkpoint if this is a retry ---
    existing_meta = document.get_file_metadata()
    resume_from_page = existing_meta.processing_checkpoint or 0
    if resume_from_page:
        logger.info(
            f"Document {document_id}: resuming from checkpoint page {resume_from_page}."
        )

    text_generator = ai_service.extract_text_from_pdf_sync_generator(file_content)

    aggregated_results: Dict[str, Any] = defaultdict(list)
    full_extracted_text = []
    page_summaries = []
    total_pages = 0
    total_input_tokens = 0
    total_output_tokens = 0
    provider_used = None

    for page_num, page_text in text_generator:
        total_pages = page_num

        # --- FIX-002: Skip already-processed pages on retry ---
        if page_num <= resume_from_page:
            logger.debug(f"Skipping page {page_num} (already checkpointed).")
            continue

        logger.info(f"Analyzing page {page_num} for document {document_id}")
        full_extracted_text.append(f"--- Page {page_num} ---\n{page_text}")

        # Analyze chunk
        chunk_analysis = ai_service.analyze_text_chunk_sync(
            page_text, document.filename, analysis_type
        )

        # --- FIX-002: Capture token usage if returned as (result, usage) tuple ---
        usage: Dict[str, Any] = {}
        if isinstance(chunk_analysis, tuple) and len(chunk_analysis) == 2:
            chunk_analysis, usage = chunk_analysis
        total_input_tokens += usage.get("input_tokens", 0)
        total_output_tokens += usage.get("output_tokens", 0)
        provider_used = provider_used or usage.get("provider")

        # Aggregate results
        keywords, categories = ai_service._extract_keywords_from_analysis(
            chunk_analysis
        )
        mappings = ai_service._extract_mappings_from_analysis(chunk_analysis)
        aggregated_results["keywords"].extend(keywords)
        aggregated_results["categories"].extend(categories)
        aggregated_results["mappings"].extend(mappings)

        # Try to find the summary in the nested structure
        if isinstance(chunk_analysis, dict):
            summary = chunk_analysis.get("summary")
            if not summary:
                doc_analysis = chunk_analysis.get("document_analysis", {})
                if isinstance(doc_analysis, dict):
                    summary = doc_analysis.get("summary")
            if summary:
                page_summaries.append(summary)

        # --- FIX-001: Emit heartbeat every N pages so the scheduler knows we're alive ---
        if db and page_num % HEARTBEAT_INTERVAL_PAGES == 0:
            _emit_heartbeat(document_id, db)

        # --- FIX-002: Persist checkpoint so a retry can resume from here ---
        # Use read-modify-write (not a raw SQL || operator) so SQLAlchemy correctly
        # handles the JSONB dict serialisation without a manual CAST.
        if db:
            try:
                from models.document import Document as _Document
                _doc = db.get(_Document, document_id)
                if _doc is not None:
                    _doc.set_metadata(processing_checkpoint=page_num)
                    db.commit()
            except Exception as cp_err:
                logger.warning(f"Checkpoint write failed for page {page_num}: {cp_err}")
                try:
                    db.rollback()
                except Exception:
                    pass

        document_service.update_document_status_sync(
            document_id, DocumentStatus.PROCESSING, progress=50
        )

    # Consolidate results
    final_keywords = list(set(aggregated_results["keywords"]))
    final_categories = list(set(aggregated_results["categories"]))
    final_mappings = aggregated_results["mappings"]
    final_summary = "\n".join(page_summaries)
    final_extracted_text = "\n\n".join(full_extracted_text)

    final_ai_analysis = {
        "schema_version": 1,
        # Freshly built here from per-page results rather than copied from any
        # single chunk_analysis, so it needs its own prompt_version stamp.
        "prompt_version": ai_service.prompt_manager.PROMPT_VERSION,
        "summary": final_summary,
        "page_count": total_pages,
        "analysis_type": "chunked_unified",
        "keyword_mappings": final_mappings,
    }

    # --- FIX-002: Build cost metadata to persist alongside document content ---
    cost_metadata = {
        "processing_cost": {
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
            "provider": provider_used or ai_service.ai_provider,
            "processed_at": datetime.utcnow().isoformat(),
        },
        "processing_checkpoint": None,  # clear checkpoint on successful completion
        # Every page went through OCR (_extract_text_from_image) to produce the
        # text handed to analyze_text_chunk_sync — record which OCR prompt
        # version produced it. See AIService.OCR_PROMPT_VERSION.
        "ocr_prompt_version": ai_service.OCR_PROMPT_VERSION,
    }

    # Update document with aggregated data.
    # Embedding is generated later in extract_document_features_task once
    # client_canonical, state, and their confidence scores are available.
    document_service.update_document_content_sync(
        document_id,
        extracted_text=final_extracted_text,
        ai_analysis=final_ai_analysis,
        keywords=final_keywords,
        categories=final_categories,
        keyword_mappings=final_mappings,
        file_type="pdf",
        **cost_metadata,
    )


def _process_document_holistically(
    document_id: int,
    document,
    document_service: DocumentService,
    ai_service: AIService,
    analysis_type: str,
):
    """Helper function for original, non-chunked processing."""
    logger.info(f"Processing document {document_id} holistically.")
    analysis_result = ai_service.analyze_document_sync(
        document.file_path, document.filename, analysis_type
    )

    if not analysis_result:
        raise ValueError("AI analysis failed")

    ai_analysis = analysis_result.get("ai_analysis", {})
    keywords, categories = ai_service._extract_keywords_from_analysis(ai_analysis)
    mappings = ai_service._extract_mappings_from_analysis(ai_analysis)

    # Embedding is generated later in extract_document_features_task once
    # client_canonical, state, and their confidence scores are available.
    document_service.update_document_content_sync(
        document_id,
        extracted_text=analysis_result.get("extracted_text"),
        ai_analysis=analysis_result.get("ai_analysis", {}),
        keywords=keywords,
        categories=categories,
        keyword_mappings=mappings,
        file_type=analysis_result.get("file_type"),
        # None for text/docx (no OCR involved); set for pdf/image. See
        # AIService.OCR_PROMPT_VERSION.
        ocr_prompt_version=analysis_result.get("ocr_prompt_version"),
    )


from database import get_db


@celery_app.task(name="enqueue_documents_task")
def enqueue_documents_task():
    """
    Celery task to find and enqueue documents that are ready for processing.
    """
    from services.scheduler_service import SchedulerService

    db = None
    try:
        db = next(get_db())
        scheduler_service = SchedulerService(db)
        logger.info("Running scheduled task to enqueue documents.")
        scheduler_service.enqueue_pending_documents()
        logger.info("Finished scheduled task to enqueue documents.")
    except Exception as e:
        logger.error(f"Error in scheduled task enqueue_documents_task: {e}")
    finally:
        if db:
            db.close()


@celery_app.task(
    name="process_document_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,  # 1-minute base; doubled on each retry
)
def process_document_task(self, document_id: int, analysis_type: str = "unified"):
    """
    Celery task to process a single document.

    Changes vs original:
    - FIX-001: Acquires a Redis distributed lock to prevent duplicate concurrent
      processing. Emits processing_heartbeat_at so the recovery scheduler can
      detect and rescue zombie tasks.
    - FIX-002: Checkpoints PDF progress per page so retries resume mid-document
      rather than restarting from page 1. Retries rate-limit errors with
      exponential backoff.
    """
    db = None
    redis_client = None
    try:
        # --- FIX-001: Idempotency guard — prevent two workers processing the same doc ---
        acquired, redis_client = _acquire_processing_lock(document_id)
        if not acquired:
            logger.info(
                f"Document {document_id} is already being processed by another worker. "
                f"Skipping to prevent duplicate LLM calls."
            )
            return False

        db = next(get_db())
        document_service = DocumentService(db)
        ai_service = AIService(db)
        storage_service = StorageService()
        preview_service = PreviewService(storage_service)

        logger.info(
            f"Starting Celery processing for document {document_id} with {analysis_type} analysis"
        )

        document = document_service.get_document_sync(document_id)
        if not document:
            logger.error(f"Document {document_id} not found")
            return False

        document_service.update_document_status_sync(
            document_id, DocumentStatus.PROCESSING, progress=10
        )
        # --- FIX-001: Record initial heartbeat so the scheduler knows processing started ---
        _emit_heartbeat(document_id, db)

        # Determine file type and processing strategy
        file_type = ai_service._get_file_type(document.filename)

        if file_type == "pdf":
            _process_pdf_document_by_page(
                document_id,
                document,
                document_service,
                ai_service,
                storage_service,
                analysis_type,
                db=db,  # passed for heartbeat + checkpoint writes
            )
        else:
            _process_document_holistically(
                document_id, document, document_service, ai_service, analysis_type
            )

        # Final steps for all types
        logger.info(f"Generating preview for document {document_id}")
        preview_path = preview_service.generate_preview_sync(document.file_path)
        if preview_path:
            logger.info(f"Preview generated successfully at: {preview_path}")
            # NOTE: We do NOT store presigned URLs in the database as they expire.
            # Preview URLs are generated on-demand when requested via /previews/{filename}
        else:
            logger.warning(f"Failed to generate preview for document {document_id}")

        document_service.update_document_status_sync(
            document_id, DocumentStatus.COMPLETED, progress=100
        )

        # Trigger feature extraction (date, client_canonical, state) now that
        # extracted_text and ai_analysis are populated. force=True ensures the
        # embedding is always regenerated when processing runs, including reprocessing.
        extract_document_features_task.delay(document_id, force=True)

        # Clear the search cache in Redis
        try:
            if settings.redis_url:
                cache_client = redis.from_url(settings.redis_url)
                search_keys = list(cache_client.scan_iter("search:*"))
                if search_keys:
                    cache_client.delete(*search_keys)
                    logger.info(f"Invalidated {len(search_keys)} search cache keys.")
        except Exception as redis_error:
            logger.error(f"Could not clear Redis cache: {redis_error}")

        logger.info(
            f"Successfully completed {analysis_type} processing for document {document_id}"
        )
        return True

    except Exception as e:
        logger.error(f"Error processing document {document_id}: {str(e)}")

        # --- FIX-002: Retry transient LLM errors with exponential backoff ---
        try:
            import anthropic as _anthropic
            import openai as _openai
            _is_rate_limit = isinstance(
                e, (_anthropic.RateLimitError, _openai.RateLimitError)
            )
        except ImportError:
            _is_rate_limit = False

        if _is_rate_limit and self.request.retries < self.max_retries:
            countdown = 60 * (2 ** self.request.retries)
            logger.warning(
                f"Rate limit hit for document {document_id}. "
                f"Retrying in {countdown}s (attempt {self.request.retries + 1}/{self.max_retries})."
            )
            # Release lock before retry so the next attempt can acquire it
            _release_processing_lock(document_id, redis_client)
            redis_client = None
            raise self.retry(exc=e, countdown=countdown)

        # Non-retryable error or max retries exceeded — mark FAILED
        if db:
            try:
                document_service = DocumentService(db)
                document_service.update_document_status_sync(
                    document_id, DocumentStatus.FAILED, progress=0, error=str(e)
                )
            except Exception as db_error:
                logger.error(
                    f"Failed to update document status after error: {db_error}"
                )
        return False
    finally:
        # Always release the lock, even on unexpected exceptions
        _release_processing_lock(document_id, redis_client)
        if db:
            db.close()


@celery_app.task(name="extract_document_features_task", bind=True, max_retries=3, default_retry_delay=60)
def extract_document_features_task(self, document_id: int, force: bool = False):
    """
    Runs feature extraction (date, client_canonical, state) for a single document
    after AI processing completes.

    Idempotent: skips documents that already have client_canonical set unless
    force=True. Pass force=True to re-extract (e.g. after canonical map updates).
    """
    db = None
    try:
        db = next(get_db())
        from models.document import Document
        document = db.get(Document, document_id)
        if not document:
            logger.error(f"extract_document_features_task: document {document_id} not found")
            return False

        if document.client_canonical is not None and not force:
            logger.info(
                f"Document {document_id} already has client_canonical — skipping "
                f"(pass force=True to re-extract)."
            )
            return True

        if not document.extracted_text:
            logger.warning(f"Document {document_id} has no extracted_text — skipping feature extraction.")
            return False

        logger.info(f"Running feature extraction for document {document_id}.")
        canonical_map = load_canonical_map_from_db(db)
        result = extract_document_features(document, canonical_map)
        meta = result.pop("_meta", {})

        for field, value in result.items():
            setattr(document, field, value)

        # Store traceability keys in file_metadata without requiring new DB columns.
        document.file_metadata = {**(document.file_metadata or {}), "feature_extraction": meta}

        db.commit()
        logger.info(
            f"Feature extraction complete for document {document_id}: "
            f"client_canonical={document.client_canonical!r}, "
            f"state={document.state!r}, "
            f"date_created={document.date_created!r}, "
            f"source={meta.get('canonical_source')!r}"
        )

        # Generate the embedding now that client_canonical, state, and their
        # confidence values are populated. This is the single authoritative embedding
        # call for the document — no embedding is generated during AI processing.
        if document.ai_analysis:
            ai_service = AIService(db)
            synthesized_text, provenance = AIService.build_embedding_text(
                document.ai_analysis,
                filename=document.filename,
                client_canonical=document.client_canonical,
                client_confidence=document.client_confidence,
                state=document.state,
                state_confidence=document.state_confidence,
            )
            embeddings = ai_service.generate_embeddings_sync(synthesized_text)
            if embeddings:
                document_service = DocumentService(db)
                document_service.update_document_embeddings_sync(
                    document_id,
                    embeddings,
                    embedding_model=AIService.EMBEDDING_MODEL,
                    embedding_version=AIService.EMBEDDING_VERSION,
                    embedding_provenance=provenance,
                )
                logger.info(f"Generated embedding for document {document_id}.")
            else:
                raise RuntimeError(f"Embedding generation returned no result for document {document_id}")

        return True

    except Exception as exc:
        logger.error(f"Feature extraction failed for document {document_id}: {exc}")
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error(f"Max retries exceeded for feature extraction on document {document_id}.")
            return False
    finally:
        if db:
            db.close()
