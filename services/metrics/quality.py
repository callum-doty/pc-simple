"""
Zone 5 — data quality and review.

Three fixes to the old panels, all of them about being honest rather than
about arithmetic:

**One NULL policy.** The old confidence bars filtered ``column.isnot(None)``,
so documents extraction never touched simply vanished and the bars could show
a healthy 90% HIGH for a corpus that was mostly unprocessed. The leaderboard
directly beneath used the opposite rule — ``ELSE 0.0`` bucketed NULL as LOW.
Same columns, same screen, incompatible policies. Here NULL is a fourth
visible segment everywhere, so the missing mass is part of the picture.

**One review definition.** The nav badge counts ``needs_review OR
needs_date_review``; the old dashboard card counted only the first. They
disagreed about the size of the same queue. This matches the badge exactly.

**One permanent caveat.** ``POST /api/review/dates/{id}`` writes
``client_confidence = "HIGH"`` when a person saves a correction, so HIGH means
"the extractor was confident" *or* "a human verified it", with no way to
separate them. The confidence panel says so on its face. Un-conflating it
needs a ``verified_by`` column, which the metrics-only scope defers.

The old "bottom-quality clients" leaderboard is deliberately not ported. Its
composite score treated NULL as zero, so it ranked clients by how little
extraction had run on them while claiming to rank them by data quality.
"""

import logging
from typing import Dict, List

from sqlalchemy import Text, cast, func, or_
from sqlalchemy.orm import Session

from models.document import Document
from services.metrics import scope
from services.metrics.envelope import Metric, MetricGroup

logger = logging.getLogger(__name__)

#: The three fields feature extraction scores. Order is the display order.
CONFIDENCE_FIELDS = [
    ("date_confidence", "Date"),
    ("client_confidence", "Client"),
    ("state_confidence", "State"),
]

#: Levels in display order. "not_extracted" is the NULL bucket and is a first
#: class segment, not an omission.
LEVELS = ["HIGH", "MEDIUM", "LOW"]

CONFIDENCE_CAVEAT = (
    "HIGH conflates machine confidence with human verification — the review "
    "endpoint writes HIGH when a person saves a correction"
)


class QualityMetrics:
    """Zone 5. Constructed per request; holds no state between calls."""

    def __init__(self, db: Session):
        self.db = db

    def _confidence_distributions(self, total: int) -> List[dict]:
        """
        One row per confidence field, four segments each.

        Gathered in a single pass: three fields times four buckets is twelve
        FILTER aggregates over one scan, rather than twelve queries that could
        each see a slightly different corpus.
        """
        columns = []
        for column_name, _ in CONFIDENCE_FIELDS:
            col = getattr(Document, column_name)
            for level in LEVELS:
                columns.append(
                    func.count(Document.id)
                    .filter(func.upper(func.trim(col)) == level)
                    .label(f"{column_name}_{level}")
                )
            columns.append(
                func.count(Document.id)
                .filter(col.is_(None))
                .label(f"{column_name}_NULL")
            )

        row = self.db.query(*columns).one()
        data = dict(row._mapping)

        out = []
        for column_name, label in CONFIDENCE_FIELDS:
            counts = {lvl: data[f"{column_name}_{lvl}"] or 0 for lvl in LEVELS}
            not_extracted = data[f"{column_name}_NULL"] or 0
            scored = sum(counts.values())
            # Anything neither a recognised level nor NULL — an unexpected
            # value. Surfaced rather than silently folded into a bucket.
            other = total - scored - not_extracted
            out.append(
                {
                    "field": column_name,
                    "label": label,
                    "high": counts["HIGH"],
                    "medium": counts["MEDIUM"],
                    "low": counts["LOW"],
                    "not_extracted": not_extracted,
                    "unrecognised": max(other, 0),
                    "total": total,
                }
            )
        return out

    def _review_queue(self) -> dict:
        """
        The review backlog, using the same definition as the nav badge.

        ``needs_date_review`` is labelled precisely: it is set only when a date
        parsed successfully and fell outside 2019-2026. A document with no
        date at all is FALSE, so this counts range violations, not absences.
        """
        row = self.db.query(
            func.count(Document.id)
            .filter(
                or_(
                    Document.needs_review.is_(True),
                    Document.needs_date_review.is_(True),
                )
            )
            .label("either"),
            func.count(Document.id)
            .filter(Document.needs_review.is_(True))
            .label("needs_review"),
            func.count(Document.id)
            .filter(Document.needs_date_review.is_(True))
            .label("date_range_violation"),
            func.count(Document.id)
            .filter(
                Document.needs_review.is_(True),
                Document.needs_date_review.is_(True),
            )
            .label("both"),
        ).one()
        return dict(row._mapping)

    def _embedding_staleness(self) -> dict:
        """
        Documents whose embedding is absent or behind the current generation.

        Uses the predicate ``backfill_embeddings.py`` already runs on, so the
        number maps directly onto an action rather than describing a state
        with no remedy attached.
        """
        try:
            from services.ai_service import AIService

            current = AIService.EMBEDDING_VERSION
        except Exception as e:
            logger.warning(f"Metrics: could not read EMBEDDING_VERSION: {e}")
            current = None

        row = self.db.query(
            func.count(Document.id)
            .filter(Document.search_vector.is_(None))
            .label("never_embedded"),
            func.count(Document.id)
            .filter(
                Document.search_vector.isnot(None),
                Document.embedding_version.is_(None),
            )
            .label("unversioned"),
        ).one()

        stale = None
        if current is not None:
            stale = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.search_vector.isnot(None),
                    Document.embedding_version.isnot(None),
                    Document.embedding_version < current,
                )
                .scalar()
                or 0
            )

        return {
            "never_embedded": row.never_embedded or 0,
            "unversioned": row.unversioned or 0,
            "behind_current": stale,
            "current_version": current,
        }

    def _curation_effort(self) -> dict:
        """
        Growth of ``canonical_overrides`` — the only durable trace of human
        work anywhere in the schema.

        Review edits clear their flags in place with no audit row, so review
        throughput is unmeasurable. Overrides are the exception: each is a
        deliberate correction with a creation timestamp.
        """
        from sqlalchemy import text

        try:
            total = (
                self.db.execute(text("SELECT COUNT(*) FROM canonical_overrides"))
                .scalar()
                or 0
            )
            recent = (
                self.db.execute(
                    text(
                        "SELECT COUNT(*) FROM canonical_overrides "
                        "WHERE created_at >= now() - interval '30 days'"
                    )
                ).scalar()
                or 0
            )
            return {"total": total, "last_30_days": recent}
        except Exception as e:
            # Table absent on an unmigrated database. Rollback is load-bearing:
            # Postgres aborts the transaction on a failed statement, so without
            # it every later query in this collector fails too.
            logger.warning(f"Metrics: could not read canonical_overrides: {e}")
            try:
                self.db.rollback()
            except Exception:
                logger.warning("Metrics: rollback after canonical_overrides read failed")
            return {"total": None, "last_30_days": None}

    def _duplicates(self) -> dict:
        """
        Content hashes appearing more than once.

        Dropbox-ingested rows only — manual uploads never set
        ``content_hash`` — so the label says so rather than implying the whole
        corpus was checked.
        """
        dup = (
            self.db.query(Document.content_hash)
            .filter(Document.content_hash.isnot(None))
            .group_by(Document.content_hash)
            .having(func.count(Document.id) > 1)
            .subquery()
        )
        groups = self.db.query(func.count()).select_from(dup).scalar() or 0
        hashed = (
            self.db.query(func.count(Document.id))
            .filter(Document.content_hash.isnot(None))
            .scalar()
            or 0
        )
        return {"duplicate_groups": groups, "hashed_documents": hashed}

    def collect(self) -> MetricGroup:
        as_of = scope.now_utc()
        total = scope.count(scope.corpus(self.db.query(Document)))

        review = self._review_queue()
        embeddings = self._embedding_staleness()
        curation = self._curation_effort()
        duplicates = self._duplicates()

        g = MetricGroup(name="quality")

        g.add(
            "review_backlog",
            Metric(
                value=review["either"],
                denominator=total,
                denominator_label="of the corpus flagged for review",
                as_of=as_of,
                scope="corpus",
            ),
        )
        g.add(
            "date_range_violations",
            Metric(
                value=review["date_range_violation"],
                denominator=total,
                denominator_label="of the corpus, a date parsed outside 2019-2026",
                as_of=as_of,
                scope="corpus",
                caveat=(
                    "counts range violations only — a document with no date at "
                    "all is not flagged"
                ),
            ),
        )
        g.add(
            "never_embedded",
            Metric(
                value=embeddings["never_embedded"],
                denominator=total,
                denominator_label="of the corpus has no embedding",
                as_of=as_of,
                scope="corpus",
            ),
        )
        if embeddings["behind_current"] is not None:
            g.add(
                "embeddings_behind",
                Metric(
                    value=embeddings["behind_current"],
                    denominator=total,
                    denominator_label=(
                        f"of the corpus embedded below version "
                        f"{embeddings['current_version']}"
                    ),
                    as_of=as_of,
                    scope="corpus",
                ),
            )
        if curation["total"] is not None:
            g.add(
                "canonical_overrides",
                Metric(
                    value=curation["total"],
                    denominator_label="manual client-name corrections on record",
                    as_of=as_of,
                    scope="canonical_overrides",
                ),
            )
        g.add(
            "duplicate_groups",
            Metric(
                value=duplicates["duplicate_groups"],
                denominator=duplicates["hashed_documents"],
                denominator_label="of hashed documents share a content hash",
                as_of=as_of,
                scope="hashed",
                caveat="only Dropbox-ingested rows carry a content hash",
            ),
        )

        g.series["confidence"] = self._confidence_distributions(total)
        g.series["review_queue"] = [
            {
                "key": "either",
                "label": "Flagged for review",
                "count": review["either"],
                "note": "Matches the nav badge exactly: needs_review OR needs_date_review.",
            },
            {
                "key": "needs_review",
                "label": "Low field confidence",
                "count": review["needs_review"],
                "note": "Weighted score across date, client and state below threshold.",
            },
            {
                "key": "date_range_violation",
                "label": "Date out of range",
                "count": review["date_range_violation"],
                "note": "A date parsed but fell outside 2019-2026. Not the same as a missing date.",
            },
            {
                "key": "both",
                "label": "Both flags",
                "count": review["both"],
                "note": "Counted once in the total above.",
            },
        ]
        g.series["embeddings"] = [embeddings]
        g.series["curation"] = [curation]
        g.note = CONFIDENCE_CAVEAT
        return g
