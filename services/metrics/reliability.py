"""
Zone 2 — processing outcomes.

The success rate here is keyed on ``status`` and nothing else. The old
dashboard computed it as::

    total_7d      = count(processed_at >= cutoff AND status IN (COMPLETED, FAILED))
    successful_7d = count(processed_at >= cutoff AND status == COMPLETED)

``processed_at`` is written only on the COMPLETED branch, so the shared
``processed_at >= cutoff`` predicate silently removed every failure from the
denominator. ``total_7d`` and ``successful_7d`` were the same query, the rate
was 100.00% by construction, and the card painted itself green regardless of
how badly processing was going.

Time bucketing needs care for the same reason: nothing records when a document
failed. ``scope.resolution_time()`` coalesces ``processed_at`` with
``updated_at`` to place a failure on the timeline, and is used *only* for the
x-axis — the numerator and denominator always come from ``status``. A
mis-bucketed failure can therefore shift a point along the trend but can never
alter the rate.
"""

import logging
from typing import List

from sqlalchemy import func
from sqlalchemy.orm import Session, load_only

from models.document import Document, DocumentStatus
from services.metrics import scope
from services.metrics.envelope import Metric, MetricGroup

logger = logging.getLogger(__name__)

#: How many days of outcome history the trend covers.
TREND_DAYS = 30

#: How many recent failures to list. Failures cannot be grouped — see the note
#: in ``collect`` — so the panel shows raw strings rather than implying a
#: taxonomy it cannot compute.
FAILURE_SAMPLE = 25


class ReliabilityMetrics:
    """Zone 2. Constructed per request; holds no state between calls."""

    def __init__(self, db: Session):
        self.db = db

    def _outcome_trend(self) -> List[dict]:
        """
        Daily completed and failed counts over the trend window.

        Bucketed on ``resolution_time()``. Both series come from one grouped
        query so the two can never be assembled over different windows — the
        old trend built uploads and completions as separate queries with
        separate filters and aligned them in JavaScript.
        """
        bucket = func.date(scope.resolution_time()).label("day")
        rows = (
            self.db.query(
                bucket,
                func.count(Document.id)
                .filter(Document.status == DocumentStatus.COMPLETED)
                .label("completed"),
                func.count(Document.id)
                .filter(Document.status == DocumentStatus.FAILED)
                .label("failed"),
            )
            .filter(
                Document.status.in_(scope.TERMINAL),
                scope.resolution_time() >= scope.ago(days=TREND_DAYS),
            )
            .group_by(bucket)
            .order_by(bucket)
            .all()
        )
        return [
            {
                "date": str(r.day),
                "completed": r.completed or 0,
                "failed": r.failed or 0,
            }
            for r in rows
        ]

    def _recent_failures(self) -> List[dict]:
        """
        Most recent documents in a FAILED state, with their raw error text.

        No grouping: ``processing_error`` is free text with no code or
        category, so quota exhaustion, OCR failure, soft-timeout kills and
        parse errors are indistinguishable. Listing the strings is honest;
        charting a breakdown would not be.
        """
        # load_only matters here: a bare query(Document) drags extracted_text
        # and the 1536-dimension search_vector across for every row, which for
        # a panel showing a filename and an error string is megabytes of
        # transfer per dashboard load.
        rows = (
            scope.failed(self.db.query(Document))
            .options(
                load_only(
                    Document.id,
                    Document.filename,
                    Document.processing_error,
                    Document.processed_at,
                    Document.updated_at,
                )
            )
            .order_by(scope.resolution_time().desc().nullslast())
            .limit(FAILURE_SAMPLE)
            .all()
        )
        return [
            {
                "id": d.id,
                "filename": d.filename,
                "error": d.processing_error,
                "resolved_at": (
                    (d.processed_at or d.updated_at).isoformat()
                    if (d.processed_at or d.updated_at)
                    else None
                ),
            }
            for d in rows
        ]

    def collect(self) -> MetricGroup:
        as_of = scope.now_utc()
        base = self.db.query(Document)

        total = scope.count(scope.corpus(base))
        terminal_count = scope.count(scope.terminal(base))
        completed_count = scope.count(scope.completed(base))
        failed_count = scope.count(scope.failed(base))

        # "Ever failed" and "currently failed" are different questions, and the
        # old review panel conflated them under the label "Processing Errors".
        # processing_error is never cleared on a subsequent success, so a
        # non-null value means the document has failed at some point in its
        # life — not that it is broken now.
        ever_failed = scope.count(
            base.filter(Document.processing_error.isnot(None))
        )
        recovered = scope.count(
            scope.completed(base).filter(Document.processing_error.isnot(None))
        )

        g = MetricGroup(name="reliability")

        g.add(
            "success_rate",
            Metric(
                value=(
                    round(completed_count / terminal_count * 100, 2)
                    if terminal_count
                    else None
                ),
                denominator=terminal_count,
                denominator_label="of documents that finished processing",
                as_of=as_of,
                scope="terminal",
            ),
        )
        g.add(
            "completed",
            Metric(
                value=completed_count,
                denominator=terminal_count,
                denominator_label="of documents that finished processing",
                as_of=as_of,
                scope="terminal",
            ),
        )
        g.add(
            "currently_failed",
            Metric(
                value=failed_count,
                denominator=terminal_count,
                denominator_label="of documents that finished processing",
                as_of=as_of,
                scope="failed",
            ),
        )
        g.add(
            "ever_failed",
            Metric(
                value=ever_failed,
                denominator=total,
                denominator_label="of the whole corpus, have ever recorded an error",
                as_of=as_of,
                scope="corpus",
                caveat=(
                    "processing_error is never cleared, so this counts documents "
                    "that have failed at some point — not documents broken now"
                ),
            ),
        )
        g.add(
            "recovered",
            Metric(
                value=recovered,
                denominator=ever_failed,
                denominator_label="of documents that ever recorded an error",
                as_of=as_of,
                scope="completed",
            ),
        )
        g.add(
            "unfinished",
            Metric(
                value=total - terminal_count,
                denominator=total,
                denominator_label="of the whole corpus, not yet finished processing",
                as_of=as_of,
                scope="active",
            ),
        )

        g.series["outcome_trend"] = self._outcome_trend()
        g.series["recent_failures"] = self._recent_failures()
        g.note = (
            "Failures carry no completion timestamp — nothing records when a "
            "document failed — so the trend buckets on "
            "COALESCE(processed_at, updated_at). The rate itself is computed "
            "from status alone, so an imprecise bucket cannot affect it."
        )
        return g
