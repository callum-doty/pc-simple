"""
Scheduler service - handles periodic tasks and throttling
"""

import logging
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import and_

from models.document import Document, DocumentStatus
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class SchedulerService:
    """
    Service for scheduling and throttling document processing tasks.
    """

    def __init__(self, db: Session):
        self.db = db

    def enqueue_pending_documents(self):
        """
        Finds documents in QUEUED status and triggers their processing,
        respecting the throttling limits.
        """
        try:
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
