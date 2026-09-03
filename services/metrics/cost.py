"""
Zone 3 — token spend.

Reads ``file_metadata.processing_cost``, which the worker has been writing
since FIX-002 and which no dashboard has ever surfaced. It is the largest
genuinely new measure available from the existing schema: input and output
tokens with a provider, per document, joinable to client and to time.

Two limits travel with every number here, and both are rendered rather than
buried:

* Only the chunked PDF path writes ``processing_cost``. Documents processed
  holistically have no token record at all, so corpus-wide totals undercount
  by the holistic share. ``coverage`` measures exactly that share.
* Figures are in tokens, not currency. No price table exists in the config,
  and a hardcoded rate would go stale silently. If dollars are wanted, add one
  rate constant and derive them in the template so there is a single source
  of truth for the price.

JSONB carries no type contract, so every numeric read is guarded by a regex
before casting — a malformed value written by an older pipeline version would
otherwise abort the whole query rather than skip one row.
"""

import logging
from typing import List

from sqlalchemy import BigInteger, Float, cast, func
from sqlalchemy.orm import Session

from models.document import Document
from services.metrics import scope
from services.metrics.jsonb import get, get_text
from services.metrics.envelope import Metric, MetricGroup

logger = logging.getLogger(__name__)

#: Only digits. Guards the cast to bigint against anything an older pipeline
#: version may have written into these keys.
NUMERIC = "^[0-9]+$"

TOP_CLIENTS = 15


def _cost(key: str):
    """
    Text accessor for one key inside processing_cost.

    Built from explicit ``->`` / ``->>`` operators rather than ORM subscripts:
    SQLAlchemy 2.0 renders ``col["k"]`` as PostgreSQL subscript syntax, which
    requires server version 14+. See services/metrics/jsonb.
    """
    return get_text(get(Document.file_metadata, "processing_cost"), key)


def _numeric(key: str):
    """The key's value as a bigint. Only valid where ``_guard`` holds."""
    return cast(_cost(key), BigInteger)


def _guard():
    """True only where input_tokens is a clean integer string."""
    return _cost("input_tokens").op("~")(NUMERIC)


def _output_guard():
    return _cost("output_tokens").op("~")(NUMERIC)


def _total_tokens():
    """
    Input plus output, treating a malformed output value as zero.

    Input is guarded separately and gates the whole row; output is coalesced
    so a row with valid input but a junk output figure still contributes its
    input rather than dropping out of the totals entirely.
    """
    return _numeric("input_tokens") + func.coalesce(
        cast(
            func.nullif(
                func.regexp_replace(_cost("output_tokens"), "[^0-9]", "", "g"), ""
            ),
            BigInteger,
        ),
        0,
    )


class CostMetrics:
    """Zone 3. Constructed per request; holds no state between calls."""

    def __init__(self, db: Session):
        self.db = db

    def _totals(self) -> dict:
        row = (
            self.db.query(
                func.count(Document.id).label("docs"),
                func.sum(_numeric("input_tokens")).label("input_tokens"),
                func.sum(
                    func.coalesce(
                        cast(
                            func.nullif(
                                func.regexp_replace(
                                    _cost("output_tokens"), "[^0-9]", "", "g"
                                ),
                                "",
                            ),
                            BigInteger,
                        ),
                        0,
                    )
                ).label("output_tokens"),
            )
            .filter(_guard())
            .one()
        )
        return {
            "docs": row.docs or 0,
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
        }

    def _percentiles(self) -> dict:
        total = cast(_total_tokens(), Float)
        row = (
            self.db.query(
                func.percentile_cont(0.5).within_group(total.asc()).label("p50"),
                func.percentile_cont(0.95).within_group(total.asc()).label("p95"),
            )
            .filter(_guard())
            .one()
        )
        return {
            "p50": None if row.p50 is None else round(float(row.p50)),
            "p95": None if row.p95 is None else round(float(row.p95)),
        }

    def _by_provider(self) -> List[dict]:
        rows = (
            self.db.query(
                _cost("provider").label("provider"),
                func.count(Document.id).label("docs"),
                func.sum(_total_tokens()).label("tokens"),
            )
            .filter(_guard())
            .group_by(_cost("provider"))
            .order_by(func.sum(_total_tokens()).desc())
            .all()
        )
        return [
            {
                "provider": r.provider or "unrecorded",
                "docs": r.docs,
                "tokens": int(r.tokens or 0),
            }
            for r in rows
        ]

    def _by_client(self) -> List[dict]:
        """
        Which clients are expensive to process.

        Scoped to documents that have both a cost record and a resolved
        client, which is a subset of a subset — the denominator label says so
        rather than letting this read as corpus-wide.
        """
        rows = (
            self.db.query(
                Document.client_canonical.label("client"),
                func.count(Document.id).label("docs"),
                func.sum(_total_tokens()).label("tokens"),
            )
            .filter(_guard(), Document.client_canonical.isnot(None))
            .group_by(Document.client_canonical)
            .order_by(func.sum(_total_tokens()).desc())
            .limit(TOP_CLIENTS)
            .all()
        )
        return [
            {
                "client": r.client,
                "docs": r.docs,
                "tokens": int(r.tokens or 0),
                "tokens_per_doc": int((r.tokens or 0) / r.docs) if r.docs else 0,
            }
            for r in rows
        ]

    def _by_month(self) -> List[dict]:
        """
        Monthly spend, bucketed on the ``processed_at`` column.

        Not on ``processing_cost.processed_at``: that nested value is an ISO
        string with no type guarantee, while the column is a real timestamptz
        the database can bucket natively.
        """
        month = func.date_trunc("month", Document.processed_at)
        rows = (
            self.db.query(
                month.label("month"),
                func.count(Document.id).label("docs"),
                func.sum(_total_tokens()).label("tokens"),
            )
            .filter(_guard(), Document.processed_at.isnot(None))
            .group_by(month)
            .order_by(month)
            .all()
        )
        return [
            {
                "month": r.month.date().isoformat() if r.month else None,
                "docs": r.docs,
                "tokens": int(r.tokens or 0),
            }
            for r in rows
        ]

    def collect(self) -> MetricGroup:
        as_of = scope.now_utc()
        total_docs = scope.count(scope.corpus(self.db.query(Document)))
        totals = self._totals()
        pct = self._percentiles()

        coverage_caveat = (
            "only the chunked PDF path records token cost, so corpus-wide "
            "totals undercount by the holistic share"
        )

        g = MetricGroup(name="cost")

        g.add(
            "coverage",
            Metric(
                value=totals["docs"],
                denominator=total_docs,
                denominator_label="of the corpus carry a token-cost record",
                as_of=as_of,
                scope="corpus",
                caveat=coverage_caveat,
            ),
        )
        g.add(
            "input_tokens",
            Metric(
                value=totals["input_tokens"],
                denominator=totals["docs"],
                denominator_label="summed over documents with a cost record",
                as_of=as_of,
                scope="with_cost",
                caveat=coverage_caveat,
            ),
        )
        g.add(
            "output_tokens",
            Metric(
                value=totals["output_tokens"],
                denominator=totals["docs"],
                denominator_label="summed over documents with a cost record",
                as_of=as_of,
                scope="with_cost",
                caveat=coverage_caveat,
            ),
        )
        g.add(
            "tokens_p50",
            Metric(
                value=pct["p50"],
                denominator=totals["docs"],
                denominator_label="documents with a cost record",
                as_of=as_of,
                scope="with_cost",
            ),
        )
        g.add(
            "tokens_p95",
            Metric(
                value=pct["p95"],
                denominator=totals["docs"],
                denominator_label="documents with a cost record",
                as_of=as_of,
                scope="with_cost",
                caveat="page count drives this — percentiles, not a mean",
            ),
        )

        g.series["by_provider"] = self._by_provider()
        g.series["by_client"] = self._by_client()
        g.series["by_month"] = self._by_month()
        g.note = (
            "Reported in tokens, not currency: no price table exists in the "
            "configuration and a hardcoded rate would go stale silently."
        )
        return g
