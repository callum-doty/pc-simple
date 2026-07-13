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
    doc.processing_error = None
    doc.created_at = overrides.get("created_at", datetime.now(timezone.utc))
    return doc


class TestRescueZombieDocuments:
    def test_resets_stale_heartbeat_zombie(self):
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

    def test_noop_when_no_zombies_found(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = []

        service = SchedulerService(db)
        rescued = service._rescue_zombie_documents()

        assert rescued == 0
        db.commit.assert_not_called()

    def test_rescues_multiple_zombies_in_one_pass(self):
        stale = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS + 5)
        zombies = [make_fake_document(id=i, processing_heartbeat_at=stale) for i in (1, 2, 3)]

        db = MagicMock()
        db.query.return_value.filter.return_value.all.return_value = zombies

        service = SchedulerService(db)
        rescued = service._rescue_zombie_documents()

        assert rescued == 3
        assert all(z.status == DocumentStatus.QUEUED for z in zombies)
        db.commit.assert_called_once()


class TestEnqueuePendingDocuments:
    def test_throttle_blocks_enqueue_when_slots_full(self, monkeypatch):
        db = MagicMock()
        db.query.return_value.filter.return_value.count.return_value = 3  # at max_concurrent

        service = SchedulerService(db)
        service._rescue_zombie_documents = Mock(return_value=0)
        monkeypatch.setattr(
            "config.get_settings",
            lambda: Mock(max_concurrent_document_processing=3),
        )
        # scheduler_service module imported settings at module load time; patch its reference too.
        import services.scheduler_service as scheduler_module
        monkeypatch.setattr(scheduler_module, "settings", Mock(max_concurrent_document_processing=3))

        service.enqueue_pending_documents()

        # Should never get as far as querying/dispatching QUEUED documents.
        db.query.return_value.filter.return_value.order_by.assert_not_called()

    def test_enqueues_available_slots_oldest_first(self, monkeypatch):
        db = MagicMock()
        # First call chain: count() of PROCESSING docs -> 0 (no throttle).
        db.query.return_value.filter.return_value.count.return_value = 0
        queued_docs = [make_fake_document(id=10), make_fake_document(id=11)]
        db.query.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = (
            queued_docs
        )

        import services.scheduler_service as scheduler_module
        monkeypatch.setattr(scheduler_module, "settings", Mock(max_concurrent_document_processing=3))

        import worker as worker_module
        fake_task = Mock()
        monkeypatch.setattr(worker_module, "process_document_task", fake_task)

        service = SchedulerService(db)
        service._rescue_zombie_documents = Mock(return_value=0)
        service.enqueue_pending_documents()

        assert fake_task.delay.call_count == 2
        fake_task.delay.assert_has_calls([call(10), call(11)], any_order=False)
        for doc in queued_docs:
            assert doc.status == DocumentStatus.PENDING


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
            worker, "_emit_heartbeat", lambda doc_id, db: heartbeat_calls.append(doc_id)
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
