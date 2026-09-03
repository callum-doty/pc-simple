"""
Zone 4 — what is actually in the archive.

The analyst view. Same subjects as the old "Intelligence Analytics" section,
with four changes:

* Every panel carries its denominator, and panels whose population is a subset
  of the corpus show the missing mass explicitly rather than implying it. The
  geography chart gets a "no state" bar; franking becomes a three-way split.
* ``state`` is trimmed. Untrimmed values exist — every read path in the
  codebase strips them — so a naive GROUP BY splits one state across rows.
* Franking stops treating "not extracted" as "not franked". ``is_frank`` is
  nullable with ``default=False`` and is only written by the feature task, so
  the old single percentage counted every unprocessed document as a negative.
* Topics are surfaced for the first time, from ``keywords.keyword_mappings``.

Three dimensions here are free and were never used: ingest source (Dropbox
cron versus manual upload), file size, and file type. All three are fully
populated on every row, which makes them the only analyst dimensions in the
schema with no coverage caveat at all.
"""

import logging
from typing import List

from sqlalchemy import Text, cast, func
from sqlalchemy.orm import Session

from models.document import Document
from services.metrics.jsonb import array_length
from services.metrics import scope
from services.metrics.envelope import Metric, MetricGroup

logger = logging.getLogger(__name__)

TOP_CLIENTS = 20
TOP_TOPICS = 15
TOP_SUBTOPICS = 15

#: File-size bands, in bytes. Political mail is mostly small scans; the bands
#: are chosen to separate single-page images from multi-page PDFs rather than
#: to be round numbers.
SIZE_BANDS = [
    (0, 250_000, "< 250 KB"),
    (250_000, 1_000_000, "250 KB – 1 MB"),
    (1_000_000, 5_000_000, "1 – 5 MB"),
    (5_000_000, 20_000_000, "5 – 20 MB"),
    (20_000_000, None, "> 20 MB"),
]


def _trimmed_state():
    return func.trim(cast(Document.state, Text))


class CorpusMetrics:
    """Zone 4. Constructed per request; holds no state between calls."""

    def __init__(self, db: Session):
        self.db = db

    # -- clients --------------------------------------------------------------

    def _top_clients(self) -> List[dict]:
        rows = (
            self.db.query(
                Document.client_canonical.label("client"),
                func.count(Document.id).label("docs"),
            )
            .filter(Document.client_canonical.isnot(None))
            .group_by(Document.client_canonical)
            .order_by(func.count(Document.id).desc())
            .limit(TOP_CLIENTS)
            .all()
        )
        return [{"client": r.client, "docs": r.docs} for r in rows]

    def _normalisation_gaps(self) -> List[dict]:
        """
        Canonical names backed by more than one raw variant.

        Measures stages 2 and 4 of the client normalisation chain disagreeing.
        This panel was already correct in the old dashboard and is carried
        over unchanged apart from its denominator label.
        """
        rows = (
            self.db.query(
                Document.client_canonical.label("client"),
                func.count(func.distinct(Document.client)).label("variants"),
                func.count(Document.id).label("docs"),
            )
            .filter(
                Document.client_canonical.isnot(None),
                Document.client.isnot(None),
            )
            .group_by(Document.client_canonical)
            .having(func.count(func.distinct(Document.client)) > 1)
            .order_by(func.count(func.distinct(Document.client)).desc())
            .limit(TOP_CLIENTS)
            .all()
        )
        return [
            {"client": r.client, "variants": r.variants, "docs": r.docs} for r in rows
        ]

    # -- geography ------------------------------------------------------------

    def _geography(self, total: int) -> List[dict]:
        """
        Documents per state, plus an explicit row for documents with no state.

        The percentage is of the whole corpus, not of documents that happen to
        have a state. The old panel divided by the latter, so a corpus where
        most documents were unextracted still showed states summing to 100%.
        """
        rows = (
            self.db.query(
                _trimmed_state().label("state"),
                func.count(Document.id).label("docs"),
            )
            .filter(Document.state.isnot(None), _trimmed_state() != "")
            .group_by(_trimmed_state())
            .order_by(func.count(Document.id).desc())
            .all()
        )
        out = [
            {
                "state": r.state,
                "docs": r.docs,
                "pct_of_corpus": round(r.docs / total * 100, 2) if total else None,
                "missing": False,
            }
            for r in rows
        ]
        placed = sum(r["docs"] for r in out)
        out.append(
            {
                "state": "no state",
                "docs": total - placed,
                "pct_of_corpus": (
                    round((total - placed) / total * 100, 2) if total else None
                ),
                "missing": True,
            }
        )
        return out

    # -- timeline -------------------------------------------------------------

    def _timeline(self) -> List[dict]:
        month = func.date_trunc("month", Document.date_created)
        rows = (
            self.db.query(month.label("month"), func.count(Document.id).label("docs"))
            .filter(Document.date_created.isnot(None))
            .group_by(month)
            .order_by(month)
            .all()
        )
        return [
            {"month": r.month.date().isoformat() if r.month else None, "docs": r.docs}
            for r in rows
        ]

    def _upload_lag_days(self):
        """
        Median days between a document's own date and its upload.

        Median rather than mean: a handful of historical backfills would drag
        an average into meaninglessness.
        """
        lag = func.extract(
            "epoch",
            Document.created_at - cast(Document.date_created, Document.created_at.type),
        ) / 86400.0
        value = (
            self.db.query(func.percentile_cont(0.5).within_group(lag.asc()))
            .filter(
                Document.date_created.isnot(None),
                Document.created_at.isnot(None),
            )
            .scalar()
        )
        return None if value is None else round(float(value), 1)

    # -- topics ---------------------------------------------------------------

    def _topic_levels(self) -> dict:
        """
        Primary categories and subcategories from one pass over the mappings.

        Unnests ``keywords.keyword_mappings`` once and aggregates both levels
        from the same CTE. Two separate LATERAL unnests — the obvious way to
        write this — walk every mapping of every document twice, which on a
        corpus of any size is the most expensive thing on the page.

        Counts DISTINCT documents: a document mentioning three economic terms
        is one document about the economy, not three.

        ``INITCAP(TRIM(...))`` collapses the casing duplicate in taxonomy.csv,
        where "Geographic & Demographic Targeting" and "...& demographic
        Targeting" are separate categories. A read-side patch — the CSV should
        be fixed at source, or the duplicate returns on the next reseed.
        """
        from sqlalchemy import text

        sql = text(
            """
            WITH mappings AS (
                SELECT
                    d.id                                           AS doc_id,
                    INITCAP(TRIM(mapping ->> 'primary_category'))  AS primary_category,
                    INITCAP(TRIM(mapping ->> 'subcategory'))       AS subcategory
                FROM documents d,
                     LATERAL jsonb_array_elements(
                         COALESCE(d.keywords -> 'keyword_mappings', '[]'::jsonb)
                     ) AS mapping
            )
            SELECT 'primary' AS level, primary_category AS name,
                   COUNT(DISTINCT doc_id) AS docs
            FROM mappings
            WHERE primary_category IS NOT NULL AND primary_category <> ''
            GROUP BY 1, 2
            UNION ALL
            SELECT 'sub' AS level, subcategory AS name,
                   COUNT(DISTINCT doc_id) AS docs
            FROM mappings
            WHERE subcategory IS NOT NULL AND subcategory <> ''
            GROUP BY 1, 2
            ORDER BY docs DESC
            """
        )
        try:
            rows = self.db.execute(sql).fetchall()
        except Exception as e:
            logger.warning(f"Metrics: topic aggregation failed: {e}")
            try:
                self.db.rollback()
            except Exception:
                logger.warning("Metrics: rollback after topic aggregation failed")
            return {"topics": [], "subtopics": []}

        topics, subtopics = [], []
        for level, name, docs in rows:
            bucket = topics if level == "primary" else subtopics
            cap = TOP_TOPICS if level == "primary" else TOP_SUBTOPICS
            if len(bucket) < cap:
                bucket.append({"name": name, "docs": docs})
        return {"topics": topics, "subtopics": subtopics}

    def _documents_with_topics(self) -> int:
        return (
            self.db.query(func.count(Document.id))
            .filter(array_length(Document.keywords, "keyword_mappings") > 0)
            .scalar()
            or 0
        )

    # -- franking -------------------------------------------------------------

    def _franking(self, total: int, extracted: int) -> dict:
        """
        Three-way split, not a percentage.

        ``is_frank`` is nullable with ``default=False`` and only the feature
        task writes it, so FALSE means "not franked" *or* "never examined".
        The old panel divided TRUE by every row and reported one number, which
        understated franking by exactly the unextracted share. Separating the
        third bucket is the only honest presentation available without making
        the column nullable-by-default.
        """
        row = self.db.query(
            func.count(Document.id).filter(Document.is_frank.is_(True)).label("franked"),
            func.count(Document.id).filter(Document.is_frank.is_(False)).label("not_franked"),
            func.count(Document.id).filter(Document.is_frank.is_(None)).label("null_frank"),
        ).one()

        franked = row.franked or 0
        return {
            "franked": franked,
            "not_franked": row.not_franked or 0,
            "null": row.null_frank or 0,
            "total": total,
            "extracted": extracted,
            # Two rates, both labelled, because the honest answer depends on
            # what you are asking. Over the corpus it is a floor; over
            # extracted documents it is the best estimate of the true rate.
            "pct_of_corpus": round(franked / total * 100, 2) if total else None,
            "pct_of_extracted": (
                round(franked / extracted * 100, 2) if extracted else None
            ),
        }

    def _frank_by_state(self) -> List[dict]:
        rows = (
            self.db.query(
                _trimmed_state().label("state"),
                func.count(Document.id).filter(Document.is_frank.is_(True)).label("franked"),
                func.count(Document.id).label("docs"),
            )
            .filter(Document.state.isnot(None), _trimmed_state() != "")
            .group_by(_trimmed_state())
            .order_by(func.count(Document.id).desc())
            .limit(20)
            .all()
        )
        return [
            {"state": r.state, "franked": r.franked or 0, "docs": r.docs} for r in rows
        ]

    # -- free dimensions ------------------------------------------------------

    def _ingest_source(self, total: int) -> List[dict]:
        """Cron ingest versus manual upload. Free, and never surfaced before."""
        dropbox = (
            self.db.query(func.count(Document.id))
            .filter(Document.dropbox_file_id.isnot(None))
            .scalar()
            or 0
        )
        return [
            {
                "source": "Dropbox cron",
                "docs": dropbox,
                "pct": round(dropbox / total * 100, 2) if total else None,
            },
            {
                "source": "Manual upload",
                "docs": total - dropbox,
                "pct": round((total - dropbox) / total * 100, 2) if total else None,
            },
        ]

    def _size_bands(self) -> List[dict]:
        """
        File size distribution. ``file_size`` is NOT NULL on every row, so this
        is the one analyst dimension with no coverage caveat.
        """
        columns = []
        for low, high, label in SIZE_BANDS:
            predicate = Document.file_size >= low
            if high is not None:
                predicate = predicate & (Document.file_size < high)
            columns.append(func.count(Document.id).filter(predicate).label(label))
        row = self.db.query(*columns).one()
        values = list(row._mapping.values())
        return [
            {"band": label, "docs": value or 0}
            for (_, _, label), value in zip(SIZE_BANDS, values)
        ]

    def _file_types(self) -> List[dict]:
        """
        Type from ``file_metadata.file_type``, falling back to the filename
        extension where the key is absent — which is most older rows.
        """
        declared = Document.file_metadata["file_type"].astext
        ext = func.lower(
            func.substring(Document.filename, r"\.([A-Za-z0-9]+)$")
        )
        label = func.coalesce(func.nullif(func.lower(declared), ""), ext, "unknown")
        rows = (
            self.db.query(label.label("kind"), func.count(Document.id).label("docs"))
            .group_by(label)
            .order_by(func.count(Document.id).desc())
            .all()
        )
        return [{"type": r.kind or "unknown", "docs": r.docs} for r in rows]

    # -- assembly -------------------------------------------------------------

    def collect(self) -> MetricGroup:
        as_of = scope.now_utc()
        total = scope.corpus_total(self.db)
        with_client = scope.count(scope.extracted(self.db.query(Document)))
        with_topics = self._documents_with_topics()
        # Reuses the count above rather than issuing a second identical scan.
        franking = self._franking(total, with_client)

        g = MetricGroup(name="corpus")

        g.add(
            "total_documents",
            Metric(
                value=total,
                denominator=total,
                denominator_label="documents in the corpus, all statuses",
                as_of=as_of,
                scope="corpus",
            ),
        )
        g.add(
            "distinct_clients",
            Metric(
                value=(
                    self.db.query(func.count(func.distinct(Document.client_canonical)))
                    .filter(Document.client_canonical.isnot(None))
                    .scalar()
                    or 0
                ),
                denominator=with_client,
                denominator_label="distinct canonical clients across documents with one",
                as_of=as_of,
                scope="extracted",
            ),
        )
        g.add(
            "distinct_states",
            Metric(
                value=(
                    self.db.query(func.count(func.distinct(_trimmed_state())))
                    .filter(Document.state.isnot(None), _trimmed_state() != "")
                    .scalar()
                    or 0
                ),
                denominator_label="distinct states present, after trimming",
                as_of=as_of,
                scope="corpus",
            ),
        )
        g.add(
            "documents_with_topics",
            Metric(
                value=with_topics,
                denominator=total,
                denominator_label="of the corpus carry at least one taxonomy mapping",
                as_of=as_of,
                scope="corpus",
            ),
        )
        g.add(
            "franked",
            Metric(
                value=franking["franked"],
                denominator=franking["extracted"],
                denominator_label="of documents the feature task resolved a client for",
                as_of=as_of,
                scope="extracted",
                caveat=(
                    "is_frank defaults to FALSE and only the feature task writes "
                    "it, so unextracted documents cannot be counted either way"
                ),
            ),
        )
        g.add(
            "upload_lag_days",
            Metric(
                value=self._upload_lag_days(),
                denominator_label="median days between document date and upload",
                as_of=as_of,
                scope="corpus",
            ),
        )

        g.series["top_clients"] = self._top_clients()
        g.series["normalisation_gaps"] = self._normalisation_gaps()
        g.series["geography"] = self._geography(total)
        g.series["timeline"] = self._timeline()
        topic_levels = self._topic_levels()
        g.series["topics"] = topic_levels["topics"]
        g.series["subtopics"] = topic_levels["subtopics"]
        g.series["franking"] = [franking]
        g.series["frank_by_state"] = self._frank_by_state()
        g.series["ingest_source"] = self._ingest_source(total)
        g.series["size_bands"] = self._size_bands()
        g.series["file_types"] = self._file_types()
        g.note = (
            "Client, state, date and franking are written by the feature task, "
            "so their populations are subsets of the corpus — each panel states "
            "its own. Ingest source, file size and file type are present on "
            "every row."
        )
        return g
