"""
Activity — status breakdown, 30-day trends, recent uploads, and search
behaviour.

This is what remains of the old ``/api/dashboard`` payload once the funnel,
reliability and corpus zones have taken their parts. Three corrections travel
with it:

**The status breakdown includes QUEUED.** The old doughnut charted PENDING,
PROCESSING, COMPLETED and FAILED, omitting the one status documents actually
arrive in. Its slices therefore did not sum to the corpus, and nothing on the
chart said so. This iterates every status and asserts the sum.

**Trends come from one grouped query per series, with an explicit date spine.**
The old version built three unaligned lists and matched them up in JavaScript
with ``Array.find`` per point, which silently dropped days where one series
had no rows.

**Search volume is labelled for what it is.** Logging happens after the Redis
cache early-return in ``search_service``, so repeat searches inside the
five-minute TTL are never recorded; and each pagination click logs its own
row. The number is simultaneously under- and over-stated, no column lets us
correct for either, and so the caveat is attached rather than implied.
"""

import logging
from datetime import timedelta
from typing import List

from sqlalchemy import func
from sqlalchemy.orm import Session, load_only

from models.document import Document, DocumentStatus
from models.search_query import SearchQuery
from services.metrics import scope
from services.metrics.envelope import Metric, MetricGroup

logger = logging.getLogger(__name__)

TREND_DAYS = 30
RECENT_UPLOADS = 10
TOP_TERMS = 10

#: The placeholder ``search_service`` writes when a search had filters but no
#: text query. Excluded from term rankings, where it would otherwise dominate,
#: and counted separately as its own (genuinely interesting) behaviour.
FILTER_ONLY_SENTINEL = "(filter only)"

SEARCH_CAVEAT = (
    "logged searches only — cache hits are never recorded and each "
    "pagination click logs a row"
)


class ActivityMetrics:
    """Constructed per request; holds no state between calls."""

    def __init__(self, db: Session):
        self.db = db

    def _status_breakdown(self, total: int) -> List[dict]:
        """
        Every status, in pipeline order, summing to the corpus.

        PENDING is included even though nothing produces it any more: rows
        still hold it, and a breakdown that claims to show everything must
        account for them.
        """
        columns = [
            func.count(Document.id)
            .filter(Document.status == status)
            .label(status)
            for status in scope.ALL_STATUSES
        ]
        row = self.db.query(*columns).one()
        data = dict(row._mapping)

        out = [
            {"status": status, "docs": data[status] or 0}
            for status in scope.ALL_STATUSES
        ]
        counted = sum(r["docs"] for r in out)
        if counted != total:
            # A status outside the known set. Surfaced as its own slice rather
            # than leaving the chart quietly failing to add up.
            out.append({"status": "OTHER", "docs": total - counted})
        return out

    def _daily(self, column, extra_filters=None, naive=False) -> List[dict]:
        cutoff = (
            scope.ago_naive(days=TREND_DAYS) if naive else scope.ago(days=TREND_DAYS)
        )
        model = SearchQuery if naive else Document
        pk = SearchQuery.id if naive else Document.id

        bucket = func.date(column).label("day")
        q = self.db.query(bucket, func.count(pk).label("docs")).filter(column >= cutoff)
        for f in extra_filters or []:
            q = q.filter(f)
        rows = q.group_by(bucket).order_by(bucket).all()
        return {str(r.day): r.docs for r in rows}

    def _trends(self) -> List[dict]:
        """
        Uploads, completions and searches on one explicit date spine.

        Building the spine here rather than in the browser means a day with no
        rows in one series is a zero rather than a missing point, and all three
        series are guaranteed the same x-axis.
        """
        uploads = self._daily(Document.created_at)
        completions = self._daily(
            Document.processed_at,
            [Document.status == DocumentStatus.COMPLETED],
        )
        searches = self._daily(SearchQuery.timestamp, naive=True)

        start = (scope.now_utc() - timedelta(days=TREND_DAYS)).date()
        spine = [start + timedelta(days=i) for i in range(TREND_DAYS + 1)]

        return [
            {
                "date": d.isoformat(),
                "uploads": uploads.get(d.isoformat(), 0),
                "completions": completions.get(d.isoformat(), 0),
                "searches": searches.get(d.isoformat(), 0),
            }
            for d in spine
        ]

    def _recent_uploads(self) -> List[dict]:
        rows = (
            self.db.query(Document)
            .options(
                load_only(
                    Document.id,
                    Document.filename,
                    Document.status,
                    Document.created_at,
                    Document.processed_at,
                )
            )
            .order_by(Document.created_at.desc().nullslast())
            .limit(RECENT_UPLOADS)
            .all()
        )
        return [
            {
                "id": d.id,
                "filename": d.filename,
                "status": d.status,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "processed_at": (
                    d.processed_at.isoformat() if d.processed_at else None
                ),
            }
            for d in rows
        ]

    def _top_terms(self) -> List[dict]:
        """
        Most frequent search terms in the trend window.

        Time-boxed and with the filter-only sentinel excluded. The old panel
        was all-time and included the sentinel, which meant a placeholder
        string generally topped the list of "search terms".
        """
        rows = (
            self.db.query(
                SearchQuery.query.label("term"),
                func.count(SearchQuery.id).label("uses"),
            )
            .filter(
                SearchQuery.timestamp >= scope.ago_naive(days=TREND_DAYS),
                SearchQuery.query != FILTER_ONLY_SENTINEL,
            )
            .group_by(SearchQuery.query)
            .order_by(func.count(SearchQuery.id).desc())
            .limit(TOP_TERMS)
            .all()
        )
        return [{"term": r.term, "uses": r.uses} for r in rows]

    def _search_summary(self) -> dict:
        cutoff = scope.ago_naive(days=TREND_DAYS)
        row = self.db.query(
            func.count(SearchQuery.id).label("total"),
            func.count(SearchQuery.id)
            .filter(SearchQuery.query == FILTER_ONLY_SENTINEL)
            .label("filter_only"),
            func.count(SearchQuery.id)
            .filter(SearchQuery.result_count == 0)
            .label("zero_result"),
            func.count(SearchQuery.id)
            .filter(SearchQuery.filter_client.isnot(None))
            .label("used_client"),
            func.count(SearchQuery.id)
            .filter(SearchQuery.filter_state.isnot(None))
            .label("used_state"),
            func.count(SearchQuery.id)
            .filter(SearchQuery.filter_date_year.isnot(None))
            .label("used_year"),
        ).filter(SearchQuery.timestamp >= cutoff).one()
        return dict(row._mapping)

    def collect(self) -> MetricGroup:
        as_of = scope.now_utc()
        total = scope.count(scope.corpus(self.db.query(Document)))
        search = self._search_summary()
        logged = search["total"] or 0

        g = MetricGroup(name="activity")

        g.add(
            "uploads_30d",
            Metric(
                value=(
                    self.db.query(func.count(Document.id))
                    .filter(Document.created_at >= scope.ago(days=TREND_DAYS))
                    .scalar()
                    or 0
                ),
                denominator=total,
                denominator_label=f"of the corpus uploaded in the last {TREND_DAYS} days",
                as_of=as_of,
                scope="corpus",
            ),
        )
        g.add(
            "searches_30d",
            Metric(
                value=logged,
                denominator_label=f"searches logged in the last {TREND_DAYS} days",
                as_of=as_of,
                scope="search_queries",
                caveat=SEARCH_CAVEAT,
            ),
        )
        g.add(
            "zero_result_searches",
            Metric(
                value=search["zero_result"] or 0,
                denominator=logged,
                denominator_label="of logged searches returned nothing",
                as_of=as_of,
                scope="search_queries",
                caveat=SEARCH_CAVEAT,
            ),
        )
        g.add(
            "filter_only_searches",
            Metric(
                value=search["filter_only"] or 0,
                denominator=logged,
                denominator_label="of logged searches used filters with no text query",
                as_of=as_of,
                scope="search_queries",
                caveat=SEARCH_CAVEAT,
            ),
        )

        g.series["status_breakdown"] = self._status_breakdown(total)
        g.series["trends"] = self._trends()
        g.series["recent_uploads"] = self._recent_uploads()
        g.series["top_terms"] = self._top_terms()
        g.series["filter_adoption"] = [
            {"filter": "client", "uses": search["used_client"] or 0},
            {"filter": "state", "uses": search["used_state"] or 0},
            {"filter": "date year", "uses": search["used_year"] or 0},
        ]
        g.note = (
            "Search figures count logged searches. Logging sits after the Redis "
            "cache early-return, so repeat searches within the 5-minute TTL are "
            "invisible, while each pagination click logs its own row."
        )
        return g
