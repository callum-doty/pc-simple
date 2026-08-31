"""
Recovery service — sweeps abandoned document-processing work back into flight.

This is a pure recovery daemon: it does not admit work. Every producer (upload,
Dropbox ingest, reprocess) dispatches its own Celery task directly, and how much
runs at once is governed by the Celery worker's --concurrency setting. The
daemon's only job is to find documents that no live task is driving any more and
put them back on the queue.

There are three ways a document ends up abandoned, and one sweep for each:

  PROCESSING with a stale heartbeat  — the worker died mid-document.
  PENDING                            — a vestigial state with no producer left.
  QUEUED with an empty broker        — its dispatch never reached the queue.

The last two exist because a document in either state has nothing that will ever
pick it up on its own. Before this daemon ran, that was permanent: 293 documents
sat in PROCESSING for up to ten months, and six sat in QUEUED, because nothing
looked for them. See docs/architecture-fixes/FIX-001.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import redis
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from models.document import Document, DocumentStatus
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# A document is considered abandoned if it has shown no sign of life for this
# long. Derived from the same settings as the worker's enforced task limit so
# the two can never drift apart — this was previously a literal anchored to a
# 300s timeout that nothing enforced. See docs/architecture-fixes/FIX-001.
ZOMBIE_THRESHOLD_SECONDS = settings.zombie_threshold_seconds


class SchedulerService:
    """
    Recovers abandoned document-processing work. See module docstring.
    """

    def __init__(self, db: Session):
        self.db = db

    # ------------------------------------------------------------------
    # Sweeps
    # ------------------------------------------------------------------

    def _rescue_zombie_documents(self) -> int:
        """
        Reset documents stuck in PROCESSING status back to QUEUED and redispatch.

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

        # Local import mirrors _dispatch below: worker imports this module inside
        # its tasks, so importing it at module scope would be circular.
        from worker import release_processing_lease

        for doc in zombie_docs:
            logger.warning(
                f"Zombie task detected: document {doc.id} has been PROCESSING since "
                f"{doc.processing_started_at} with last heartbeat "
                f"{doc.processing_heartbeat_at}. Resetting to QUEUED."
            )
            doc.status = DocumentStatus.QUEUED
            doc.processing_heartbeat_at = None
            doc.processing_started_at = None
            doc.processing_error = (
                f"Reset from zombie PROCESSING state by scheduler at "
                f"{datetime.now(timezone.utc).isoformat()}"
            )
            # FIX-001 Part C: drop the dead worker's lease so the redispatch can
            # claim it straight away rather than being turned away by a lease
            # whose owner no longer exists.
            release_processing_lease(doc.id)

        # Commit before dispatching, so a worker that picks the task up
        # immediately cannot read the stale PROCESSING status.
        self.db.commit()
        self._dispatch(zombie_docs)
        logger.info(f"Rescued {len(zombie_docs)} zombie document(s).")
        return len(zombie_docs)

    def _rescue_stranded_pending_documents(self) -> int:
        """
        Reset documents stuck in PENDING back to QUEUED and redispatch.

        PENDING has no producer any more — it was the momentary state the old
        throttled scheduler used between claiming a document and dispatching it,
        and that path is gone. It survives as the model's historical default and
        in old rows, so this sweep exists to make sure a document that lands
        there is not invisible forever: the zombie sweep only queries
        PROCESSING, and the undispatched sweep only queries QUEUED.

        Returns the number of documents rescued.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS)

        stranded_docs = (
            self.db.query(Document)
            .filter(
                Document.status == DocumentStatus.PENDING,
                or_(
                    Document.updated_at < cutoff,
                    # Never updated since insert — fall back to creation time.
                    and_(
                        Document.updated_at.is_(None),
                        Document.created_at < cutoff,
                    ),
                ),
            )
            .all()
        )

        if not stranded_docs:
            return 0

        for doc in stranded_docs:
            logger.warning(
                f"Stranded task detected: document {doc.id} has been PENDING since "
                f"{doc.updated_at or doc.created_at} with no worker claiming it. "
                f"Resetting to QUEUED."
            )
            doc.status = DocumentStatus.QUEUED
            doc.processing_heartbeat_at = None
            doc.processing_started_at = None
            doc.processing_error = (
                f"Reset from stranded PENDING state by scheduler at "
                f"{datetime.now(timezone.utc).isoformat()}"
            )

        self.db.commit()
        self._dispatch(stranded_docs)
        logger.info(f"Rescued {len(stranded_docs)} stranded PENDING document(s).")
        return len(stranded_docs)

    def _rescue_undispatched_documents(self) -> int:
        """
        Redispatch QUEUED documents that no longer have a task behind them.

        Producers set QUEUED and then dispatch, so the two can come apart if the
        dispatch itself fails — which is how six documents ended up sitting in
        QUEUED indefinitely with nothing to run them.

        Only runs when the broker queue is empty. A QUEUED document is otherwise
        indistinguishable from one waiting its turn behind a bulk load, and
        redispatching those would amplify a 7,000-document backlog into many
        thousands of duplicate messages every cycle. An empty queue means
        nothing is waiting, so anything still QUEUED is genuinely orphaned.

        Returns the number of documents redispatched.
        """
        backlog = self._broker_backlog()
        if backlog is None:
            logger.debug("Broker depth unknown — skipping undispatched sweep.")
            return 0
        if backlog > 0:
            logger.debug(
                f"Broker still holds {backlog} message(s) — skipping undispatched "
                f"sweep so queued work is not redispatched while it drains."
            )
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=ZOMBIE_THRESHOLD_SECONDS)

        orphaned_docs = (
            self.db.query(Document)
            .filter(
                Document.status == DocumentStatus.QUEUED,
                Document.created_at < cutoff,
            )
            .order_by(Document.created_at)
            .all()
        )

        if not orphaned_docs:
            return 0

        for doc in orphaned_docs:
            logger.warning(
                f"Undispatched document detected: {doc.id} has been QUEUED since "
                f"{doc.created_at} with an empty broker queue. Redispatching."
            )

        self._dispatch(orphaned_docs)
        logger.info(f"Redispatched {len(orphaned_docs)} undispatched document(s).")
        return len(orphaned_docs)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _broker_backlog(self) -> Optional[int]:
        """
        Depth of the Celery queue, or None if it cannot be determined.

        None and 0 are deliberately different: an unreadable broker means we do
        not know whether work is waiting, so the caller skips rather than risks
        redispatching a live backlog.
        """
        try:
            from worker import celery_app

            queue_name = celery_app.conf.task_default_queue or "celery"
            client = redis.from_url(settings.redis_url)
            return client.llen(queue_name)
        except Exception as e:
            logger.warning(f"Could not read broker queue depth: {e}")
            return None

    def _dispatch(self, documents: List[Document]) -> None:
        """
        Hand documents to the worker.

        A dispatch failure is left QUEUED rather than raised: the undispatched
        sweep will pick it up on a later cycle once the broker is reachable, so
        a transient broker outage cannot strand a document permanently.
        """
        from worker import process_document_task

        for doc in documents:
            try:
                process_document_task.delay(doc.id)
                logger.info(f"Dispatched document {doc.id} for processing.")
            except Exception as e:
                logger.error(
                    f"Could not dispatch document {doc.id}: {e}. Left QUEUED for a "
                    f"later recovery cycle."
                )

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run_recovery_cycle(self) -> dict:
        """
        Run all three recovery sweeps. Called on a schedule by Celery beat.

        Returns a count per sweep so the caller can log or alert on it. A
        persistently non-zero count means documents are being abandoned faster
        than they are finishing, which is worth an alarm — the failure this
        whole daemon exists to stop went unnoticed for ten months precisely
        because nothing counted it.
        """
        counts = {"zombie": 0, "pending": 0, "undispatched": 0}
        try:
            counts["zombie"] = self._rescue_zombie_documents()
            counts["pending"] = self._rescue_stranded_pending_documents()
            counts["undispatched"] = self._rescue_undispatched_documents()

            if any(counts.values()):
                logger.warning(f"Recovery cycle recovered documents: {counts}")
            else:
                logger.info("Recovery cycle: nothing to recover.")
        except Exception as e:
            logger.error(f"Error during recovery cycle: {e}")
            self.db.rollback()

        return counts
