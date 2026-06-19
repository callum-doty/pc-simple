"""
Scheduler service - handles periodic tasks and throttling
"""

import logging
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from models.document import Document, DocumentStatus
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# A PROCESSING document is considered a zombie if its heartbeat has not been
# refreshed within this many seconds. Must exceed the maximum task duration.
# See docs/architecture-fixes/FIX-001.
ZOMBIE_THRESHOLD_SECONDS = 360  # task timeout (300) + 60s grace period


class SchedulerService:
    """
    Service for scheduling and throttling document processing tasks.
    """

    def __init__(self, db: Session):
        self.db = db

    def _rescue_zombie_documents(self) -> int:
        """
        Reset documents that are stuck in PROCESSING status back to QUEUED.

        A document is a zombie if:
          - status = PROCESSING, AND
          - processing_heartbeat_at < NOW() - ZOMBIE_THRESHOLD_SECONDS, OR
          - processing_heartbeat_at IS NULL (pre-FIX-001 documents stuck in PROCESSING)

        For NULL-heartbeat documents we fall back to processing_started_at.
        Returns the number of documents rescued.
        """
        zombie_cutoff = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS)

        zombie_docs = (
            self.db.query(Document)
            .filter(
                Document.status == DocumentStatus.PROCESSING,
                or_(
                    # Has heartbeat but it's stale
                    and_(
                        Document.processing_heartbeat_at.isnot(None),
                        Document.processing_heartbeat_at < zombie_cutoff,
                    ),
                    # No heartbeat — fall back to processing_started_at
                    and_(
                        Document.processing_heartbeat_at.is_(None),
                        or_(
                            Document.processing_started_at < zombie_cutoff,
                            Document.processing_started_at.is_(None),
                        ),
                    ),
                ),
            )
            .all()
        )

        if not zombie_docs:
            return 0

        for doc in zombie_docs:
            logger.warning(
                f"Zombie task detected: document {doc.id} has been PROCESSING since "
                f"{doc.processing_started_at} with last heartbeat "
                f"{doc.processing_heartbeat_at}. Resetting to QUEUED."
            )
            doc.status = DocumentStatus.QUEUED
            doc.processing_heartbeat_at = None
            doc.processing_error = (
                f"Reset from zombie PROCESSING state by scheduler at "
                f"{datetime.now(timezone.utc).isoformat()}"
            )

        self.db.commit()
        logger.info(f"Rescued {len(zombie_docs)} zombie document(s).")
        return len(zombie_docs)

    def enqueue_pending_documents(self):
        """
        Finds documents in QUEUED status and triggers their processing,
        respecting the throttling limits.

        Also rescues zombie PROCESSING documents (FIX-001) before counting
        active processing slots, so rescued documents are included in the
        next scheduling cycle.
        """
        try:
            # --- FIX-001: Rescue zombie tasks before counting active slots ---
            rescued = self._rescue_zombie_documents()
            if rescued:
                logger.info(f"Recovered {rescued} zombie document(s) before scheduling.")

            # Get the maximum number of concurrent processing jobs from settings
            max_concurrent = settings.max_concurrent_document_processing

            # Count how many documents are currently in the PROCESSING state
            currently_processing = (
                self.db.query(Document)
                .filter(Document.status == DocumentStatus.PROCESSING)
                .count()
            )

            # Calculate how many new documents we can start processing
            available_slots = max_concurrent - currently_processing
            if available_slots <= 0:
                logger.info(
                    f"Throttling: {currently_processing}/{max_concurrent} processing slots are full. No new documents will be enqueued."
                )
                return

            # Find documents that are in the QUEUED state, oldest first
            documents_to_process = (
                self.db.query(Document)
                .filter(Document.status == DocumentStatus.QUEUED)
                .order_by(Document.created_at)
                .limit(available_slots)
                .all()
            )

            if not documents_to_process:
                logger.info("No documents in QUEUED status to process.")
                return

            from worker import process_document_task

            for doc in documents_to_process:
                # Update status to PENDING to signify it's about to be processed
                doc.status = DocumentStatus.PENDING
                self.db.commit()

                # Dispatch the background task
                process_document_task.delay(doc.id)
                logger.info(f"Enqueued document {doc.id} for processing.")

            logger.info(
                f"Successfully enqueued {len(documents_to_process)} documents for processing."
            )

        except Exception as e:
            logger.error(f"Error in scheduler service while enqueuing documents: {e}")
            self.db.rollback()
