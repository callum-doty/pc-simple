"""
Zone 0 — live operational state.

Six tiles, each a *pair* of numbers whose agreement is the signal rather than
either figure alone. The catalog established that the Redis processing lease
is more trustworthy than ``status='PROCESSING'`` (a lease is held by a live
task and expires on its own) and that the Celery queue is more trustworthy
than the database backlog (it is what a worker will actually pick up next).

Rather than pick the better number and discard the other, both are shown and
their divergence is interpreted. A surplus of PROCESSING rows over leases is a
worker that died; a surplus of database backlog over broker depth is a
dispatch that never landed. Neither is visible from one figure.

Cheap by design — Redis reads plus indexed counts — so the zone can be polled
without loading the database.
"""

import logging
from typing import Optional

import redis as redis_lib
from sqlalchemy import func
from sqlalchemy.orm import Session

from config import get_settings
from models.document import Document
from services.metrics import scope
from services.metrics.envelope import (
    Metric,
    MetricGroup,
    verdict_duration,
    verdict_in_flight,
    verdict_ingest_freshness,
    verdict_pending_work,
    verdict_queue_wait,
    verdict_zombies,
)

logger = logging.getLogger(__name__)
settings = get_settings()

#: Matches worker._lease_key(). Not imported from worker: that module builds a
#: Celery app and pulls in the whole processing stack at import time, which a
#: web request should not pay for. Kept in a named constant so the coupling is
#: at least explicit.
LEASE_KEY_PATTERN = "doc_processing_lock:*"

#: The Dropbox ingest cron interval from render.yaml ("*/10 * * * *").
INGEST_INTERVAL_SECONDS = 600

#: Upload window for the queue-wait percentiles. See ``_queue_wait_stats`` for
#: why this cannot be all-time: requeues reset ``processing_started_at`` but
#: not ``created_at``, so a replayed document otherwise reports its whole age
#: as a queue wait.
QUEUE_WAIT_WINDOW_DAYS = 7


class NowMetrics:
    """Zone 0. Constructed per request; holds no state between calls."""

    def __init__(self, db: Session):
        self.db = db
        self._redis: Optional[redis_lib.Redis] = None
        self._redis_tried = False

    # -- infrastructure reads -------------------------------------------------

    @property
    def redis(self) -> Optional[redis_lib.Redis]:
        """
        A Redis client, or None if unreachable.

        None is a first-class answer here rather than an error: every caller
        distinguishes "unreachable" from "zero", because reporting an
        unreadable broker as an empty queue is how you conclude the pipeline
        is idle while a backlog drains nowhere.
        """
        if self._redis_tried:
            return self._redis
        self._redis_tried = True
        if not settings.redis_url:
            return None
        try:
            client = redis_lib.from_url(settings.redis_url, decode_responses=True)
            client.ping()
            self._redis = client
        except Exception as e:
            logger.warning(f"Metrics: Redis unreachable, live gauges degraded: {e}")
            self._redis = None
        return self._redis

    def _lease_count(self) -> Optional[int]:
        """
        Number of documents holding a live processing lease.

        SCAN rather than KEYS — this runs on a web request against a shared
        Redis that also holds the session store and the search cache. The
        lease keyspace is bounded by worker concurrency, so the scan is small.
        """
        client = self.redis
        if client is None:
            return None
        try:
            return sum(1 for _ in client.scan_iter(match=LEASE_KEY_PATTERN, count=100))
        except Exception as e:
            logger.warning(f"Metrics: could not scan processing leases: {e}")
            return None

    def _broker_depth(self) -> Optional[int]:
        """
        Depth of the Celery queue, or None if it cannot be read.

        Delegates to SchedulerService, which already implements this and
        already documents that None and 0 are deliberately different. Calling
        it rather than reimplementing keeps one definition of "how much work
        is queued" for both the daemon and the dashboard.
        """
        try:
            from services.scheduler_service import SchedulerService

            return SchedulerService(self.db)._broker_backlog()
        except Exception as e:
            logger.warning(f"Metrics: could not read broker depth: {e}")
            return None

    def _ingest_cursor_age_seconds(self) -> Optional[float]:
        """
        Age of the Dropbox sync cursor.

        ``dropbox_sync_state`` is a single-row table created by raw SQL in the
        migration and has no SQLAlchemy model, hence the textual query. Its
        ``updated_at`` is the only persisted evidence that the ingest cron is
        running at all.
        """
        from sqlalchemy import text

        try:
            row = self.db.execute(
                text(
                    "SELECT EXTRACT(EPOCH FROM (now() - updated_at)) "
                    "FROM dropbox_sync_state WHERE id = 1"
                )
            ).fetchone()
        except Exception as e:
            # An unmigrated database has no such table — the migration creates
            # it in raw SQL and there is no model to fall back on. Degrade this
            # one tile rather than the endpoint.
            #
            # The rollback is load-bearing, not tidiness: Postgres aborts the
            # whole transaction on a failed statement, so without it every
            # query after this one in collect() raises InFailedSqlTransaction
            # and the defensive branch turns a missing tile into a 500.
            logger.warning(f"Metrics: could not read dropbox_sync_state: {e}")
            try:
                self.db.rollback()
            except Exception:
                logger.warning("Metrics: rollback after dropbox_sync_state read failed")
            return None
        if not row or row[0] is None:
            return None
        return float(row[0])

    # -- percentiles ----------------------------------------------------------

    # Percentiles rather than means throughout: the Celery soft limit
    # truncates the distribution — a task past ``processing_timeout`` is
    # killed and the document marked FAILED — so the tail is clipped and an
    # average reads far healthier than the experience. The old dashboard
    # reported the mean.

    def _status_counts(self) -> dict:
        """
        Corpus size, in-progress, backlog, zombies and the legacy-timing
        exclusion — in a single pass.

        The zombie predicate is the scheduler's, reused verbatim from
        ``scope.zombies`` so the dashboard reports exactly the set the recovery
        daemon will act on.
        """
        from sqlalchemy import and_, or_

        cutoff = scope.zombie_cutoff()
        zombie = and_(
            Document.status == "PROCESSING",
            or_(
                and_(
                    Document.processing_heartbeat_at.isnot(None),
                    Document.processing_heartbeat_at < cutoff,
                ),
                and_(
                    Document.processing_heartbeat_at.is_(None),
                    or_(
                        Document.processing_started_at < cutoff,
                        Document.processing_started_at.is_(None),
                    ),
                ),
            ),
        )
        row = self.db.query(
            func.count().label("total"),
            func.count().filter(Document.status == "PROCESSING").label("processing"),
            func.count().filter(Document.status.in_(scope.BACKLOG)).label("backlog"),
            func.count().filter(zombie).label("zombies"),
            func.count()
            .filter(
                Document.status == "COMPLETED",
                Document.processing_started_at.is_(None),
            )
            .label("legacy_timing"),
        ).select_from(Document).one()
        return {k: (v or 0) for k, v in row._mapping.items()}

    def _duration_stats(self) -> dict:
        """True worker duration: first PROCESSING transition to completion."""
        seconds = func.extract(
            "epoch", Document.processed_at - Document.processing_started_at
        )
        q = scope.with_timings(
            self.db.query(
                func.percentile_cont(0.5).within_group(seconds.asc()).label("p50"),
                func.percentile_cont(0.95).within_group(seconds.asc()).label("p95"),
                func.count(Document.id).label("n"),
            )
        )
        row = q.one()
        return {
            "p50": None if row.p50 is None else round(float(row.p50), 1),
            "p95": None if row.p95 is None else round(float(row.p95), 1),
            "n": row.n or 0,
        }

    def _queue_wait_stats(self) -> dict:
        """
        Time from upload to the worker picking the document up, over documents
        uploaded in the last :data:`QUEUE_WAIT_WINDOW_DAYS` days.

        Separately meaningful from processing duration: a rising queue wait
        with flat worker duration means the answer is more concurrency, not
        optimisation. Nothing on the old dashboard distinguished the two.

        WHY THIS IS WINDOWED

        ``processing_started_at`` is cleared on every requeue — the zombie
        sweep, the reprocess endpoint and ``scripts/replay_backlog.py`` all
        null it so the next run is measured on its own — while ``created_at``
        never moves. For a document replayed long after upload the difference
        is therefore the document's *age*, not the time it waited to be picked
        up.

        Unwindowed, that made the historical backlog replay the dominant
        population: the fossil cohort (created 2025-08-05 to 2026-05-20, see
        scripts/replay_backlog.py) reported a p50 queue wait of ~127 days and
        the tile concluded "worker capacity is the limit" about a pipeline
        that was keeping up fine. A document uploaded inside the window has no
        room to report a months-long wait, so the figure describes current
        queue latency, which is the only thing the tile is read for.

        This measures documents that *have* started. A document still waiting
        is not in the population at all — the backlog pair above is what shows
        those, and it is the honest place to look for work that never started.
        """
        seconds = func.extract(
            "epoch", Document.processing_started_at - Document.created_at
        )
        row = (
            self.db.query(
                func.percentile_cont(0.5).within_group(seconds.asc()).label("p50"),
                func.percentile_cont(0.95).within_group(seconds.asc()).label("p95"),
                func.count(Document.id).label("n"),
            )
            .filter(
                Document.processing_started_at.isnot(None),
                Document.created_at.isnot(None),
                Document.created_at >= scope.ago(days=QUEUE_WAIT_WINDOW_DAYS),
            )
            .one()
        )
        return {
            "p50": None if row.p50 is None else round(float(row.p50), 1),
            "p95": None if row.p95 is None else round(float(row.p95), 1),
            "n": row.n or 0,
        }

    # -- assembly -------------------------------------------------------------

    def collect(self) -> MetricGroup:
        as_of = scope.now_utc()
        base = self.db.query(Document)

        # One scan for every status figure. This endpoint is polled, so five
        # separate COUNT(*) scans every 15 seconds was the single most
        # repeated cost on the deployment.
        counts = self._status_counts()
        total = counts["total"]
        processing_count = counts["processing"]
        backlog_count = counts["backlog"]
        zombie_count = counts["zombies"]
        lease_count = self._lease_count()
        broker_depth = self._broker_depth()
        cursor_age = self._ingest_cursor_age_seconds()
        duration = self._duration_stats()
        queue_wait = self._queue_wait_stats()
        threshold = scope.zombie_threshold_seconds()
        soft_limit = scope.soft_time_limit_seconds()

        g = MetricGroup(name="now")

        g.add(
            "lease_count",
            Metric(
                value=lease_count,
                denominator_label="documents holding a live Redis processing lease",
                as_of=as_of,
                scope="infrastructure",
                caveat=None if lease_count is not None else "Redis unreachable",
            ),
        )
        g.add(
            "processing_count",
            Metric(
                value=processing_count,
                denominator=total,
                denominator_label="documents marked PROCESSING, of the whole corpus",
                as_of=as_of,
                scope="processing",
            ),
        )
        g.add(
            "broker_depth",
            Metric(
                value=broker_depth,
                denominator_label="tasks waiting in the Celery queue",
                as_of=as_of,
                scope="infrastructure",
                caveat=None if broker_depth is not None else "broker unreadable",
            ),
        )
        g.add(
            "db_backlog",
            Metric(
                value=backlog_count,
                denominator=total,
                denominator_label="documents QUEUED or PENDING, of the whole corpus",
                as_of=as_of,
                scope="backlog",
            ),
        )
        g.add(
            "zombies",
            Metric(
                value=zombie_count,
                denominator=processing_count,
                denominator_label=(
                    f"of documents marked PROCESSING, silent beyond {threshold}s"
                ),
                as_of=as_of,
                scope="zombies",
            ),
        )
        g.add(
            "ingest_cursor_age_seconds",
            Metric(
                value=None if cursor_age is None else round(cursor_age, 0),
                denominator_label="seconds since the Dropbox sync cursor last advanced",
                as_of=as_of,
                scope="infrastructure",
            ),
        )
        g.add(
            "worker_p50_seconds",
            Metric(
                value=duration["p50"],
                denominator=duration["n"],
                denominator_label="completed documents with both timing endpoints",
                as_of=as_of,
                scope="with_timings",
            ),
        )
        g.add(
            "worker_p95_seconds",
            Metric(
                value=duration["p95"],
                denominator=duration["n"],
                denominator_label="completed documents with both timing endpoints",
                as_of=as_of,
                scope="with_timings",
            ),
        )
        g.add(
            "queue_wait_p50_seconds",
            Metric(
                value=queue_wait["p50"],
                denominator=queue_wait["n"],
                denominator_label=(
                    f"documents uploaded in the last {QUEUE_WAIT_WINDOW_DAYS} "
                    f"days that have started processing"
                ),
                as_of=as_of,
                scope="corpus",
            ),
        )
        g.add(
            "queue_wait_p95_seconds",
            Metric(
                value=queue_wait["p95"],
                denominator=queue_wait["n"],
                denominator_label=(
                    f"documents uploaded in the last {QUEUE_WAIT_WINDOW_DAYS} "
                    f"days that have started processing"
                ),
                as_of=as_of,
                scope="corpus",
            ),
        )

        g.series["verdicts"] = [
            {
                "key": "in_flight",
                "label": "In flight",
                "rows": [
                    {"label": "redis leases", "value": lease_count},
                    {"label": "status=PROCESSING", "value": processing_count},
                ],
                **verdict_in_flight(lease_count, processing_count).as_dict(),
            },
            {
                "key": "pending_work",
                "label": "Pending work",
                "rows": [
                    {"label": "broker queue", "value": broker_depth},
                    {"label": "db backlog", "value": backlog_count},
                ],
                **verdict_pending_work(broker_depth, backlog_count).as_dict(),
            },
            {
                "key": "zombies",
                "label": "Zombies",
                "rows": [
                    {"label": f"silent > {threshold}s", "value": zombie_count},
                    {"label": "marked processing", "value": processing_count},
                ],
                **verdict_zombies(zombie_count, threshold).as_dict(),
            },
            {
                "key": "ingest",
                "label": "Dropbox ingest",
                "rows": [
                    {
                        "label": "cursor age",
                        "value": None if cursor_age is None else round(cursor_age),
                        "unit": "s",
                    },
                    {"label": "cron interval", "value": INGEST_INTERVAL_SECONDS, "unit": "s"},
                ],
                **verdict_ingest_freshness(
                    cursor_age, INGEST_INTERVAL_SECONDS
                ).as_dict(),
            },
            {
                "key": "worker_duration",
                "label": "Worker duration",
                "rows": [
                    {"label": "p50", "value": duration["p50"], "unit": "s"},
                    {"label": "p95", "value": duration["p95"], "unit": "s"},
                ],
                **verdict_duration(duration["p95"], soft_limit).as_dict(),
            },
            {
                "key": "queue_wait",
                "label": "Queue wait",
                "rows": [
                    {"label": "p50", "value": queue_wait["p50"], "unit": "s"},
                    {"label": "p95", "value": queue_wait["p95"], "unit": "s"},
                ],
                **verdict_queue_wait(queue_wait["p95"]).as_dict(),
            },
        ]

        g.series["thresholds"] = [
            {"key": "zombie_threshold_seconds", "value": threshold},
            {"key": "soft_time_limit_seconds", "value": soft_limit},
            {"key": "ingest_interval_seconds", "value": INGEST_INTERVAL_SECONDS},
        ]

        completed_without_timings = counts["legacy_timing"]
        if completed_without_timings:
            g.note = (
                f"{completed_without_timings:,} completed documents predate "
                f"processing_started_at and are excluded from the duration "
                f"percentiles. There is deliberately no created_at fallback — "
                f"that would fold queue wait into processing time."
            )

        return g
