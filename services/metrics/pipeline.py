"""
Zone 1 — the pipeline funnel and extraction coverage.

The spine of the dashboard. Replaces ``get_incomplete_documents``, whose four
``LIMIT 100`` queries reported ``len()`` as a count, and whose numbers
contradicted the review panel rendered directly below them.

Two blocks, drawn differently on purpose:

* The **funnel** is sequential and cumulative. "Reached embedding" means the
  document has everything up to and including an embedding, so the bars are
  monotonic by construction. The drop between two bars is the actionable
  number — it is exactly the population ``/api/metrics/stage/{key}`` lists.

* **Coverage** is four independent bars. Client, state and date are parallel
  outputs of one task; a document can have a state and no date. Drawing them
  as funnel steps would assert a gating relationship that does not exist.

Every count is gathered in a single pass using FILTER aggregates. Sixteen
separate counts would be sixteen scans of a table this size; one scan with
sixteen filters is the same information for a fraction of the cost, and it
also guarantees every number describes the same instant.
"""

import logging
from typing import Dict, List

from sqlalchemy import and_, func
from sqlalchemy.orm import Session

from models.document import Document
from services.metrics import scope, stages
from services.metrics.envelope import Metric, MetricGroup

logger = logging.getLogger(__name__)


class PipelineMetrics:
    """Zone 1. Constructed per request; holds no state between calls."""

    def __init__(self, db: Session):
        self.db = db

    def _gather_counts(self) -> Dict[str, int]:
        """
        Every funnel and coverage count in one table scan.

        For each stage two numbers are collected:

        ``reached_*``
            The cumulative count — passed this stage and every prior one.
        ``has_*``
            The independent count — holds this stage's artifact regardless of
            whether earlier ones are present.

        Both are kept because their divergence is itself a finding. An
        independent count above the cumulative one means documents carry a
        later artifact while missing an earlier one — an embedding with no
        extracted text, say — which is a data-integrity signal that is
        completely invisible if you only compute one of the two.
        """
        columns = [func.count(Document.id).label("total")]

        for st in stages.FUNNEL_STAGES:
            if st.is_root:
                continue
            reached = stages.reached_criteria(st)
            columns.append(
                func.count(Document.id)
                .filter(and_(*reached))
                .label(f"reached_{st.key}")
            )
            columns.append(
                func.count(Document.id)
                .filter(st.predicate())
                .label(f"has_{st.key}")
            )

        for st in stages.COVERAGE_STAGES:
            columns.append(
                func.count(Document.id)
                .filter(st.predicate())
                .label(f"has_{st.key}")
            )

        row = self.db.query(*columns).one()
        return {k: (v or 0) for k, v in row._mapping.items()}


    def _funnel(self, counts: Dict[str, int], total: int, as_of) -> List[dict]:
        """
        Funnel rows, each carrying its drop from the stage above.

        ``lost_here`` is the drop, and it is also exactly what the drill-down
        endpoint lists for that stage — the same predicate produces both, so
        the bar and the list cannot disagree.
        """
        rows = []
        previous = total

        for st in stages.FUNNEL_STAGES:
            reached = total if st.is_root else counts[f"reached_{st.key}"]
            independent = None if st.is_root else counts[f"has_{st.key}"]

            row = {
                "key": st.key,
                "label": st.label,
                "reached": reached,
                "pct_of_corpus": round(reached / total * 100, 2) if total else None,
                "lost_here": max(previous - reached, 0),
                "reading": st.reading,
                "missing_label": st.missing_label,
                "drillable": not st.is_root,
            }

            # Surface out-of-order data rather than letting the cumulative
            # count quietly absorb it.
            if independent is not None and independent > reached:
                row["out_of_order"] = independent - reached
                row["out_of_order_note"] = (
                    f"{independent - reached:,} documents hold this artifact "
                    f"but are missing an earlier stage"
                )

            rows.append(row)
            previous = reached

        return rows

    def _coverage(self, counts: Dict[str, int], total: int) -> List[dict]:
        return [
            {
                "key": st.key,
                "label": st.label,
                "present": counts[f"has_{st.key}"],
                "pct_of_corpus": (
                    round(counts[f"has_{st.key}"] / total * 100, 2) if total else None
                ),
                "missing": total - counts[f"has_{st.key}"],
                "reading": st.reading,
                "missing_label": st.missing_label,
                "drillable": True,
            }
            for st in stages.COVERAGE_STAGES
        ]

    def collect(self) -> MetricGroup:
        as_of = scope.now_utc()
        counts = self._gather_counts()
        total = counts["total"]

        g = MetricGroup(name="pipeline")

        g.add(
            "corpus",
            Metric(
                value=total,
                denominator=total,
                denominator_label="documents in the corpus, all statuses",
                as_of=as_of,
                scope="corpus",
            ),
        )

        # The funnel's floor: fully processed through to an embedding. This is
        # the honest answer to "how much of the archive actually works" — it is
        # searchable semantically, has a real summary, and carries taxonomy
        # mappings. Status alone says nothing about any of that.
        fully_processed = counts[f"reached_{stages.FUNNEL_STAGES[-1].key}"]
        g.add(
            "fully_processed",
            Metric(
                value=fully_processed,
                denominator=total,
                denominator_label="of the corpus, through every pipeline stage",
                as_of=as_of,
                scope="corpus",
            ),
        )

        for st in stages.COVERAGE_STAGES:
            g.add(
                f"coverage_{st.key}",
                Metric(
                    value=counts[f"has_{st.key}"],
                    denominator=total,
                    denominator_label=f"of the corpus, {st.label.lower()} present",
                    as_of=as_of,
                    scope="corpus",
                ),
            )

        g.series["funnel"] = self._funnel(counts, total, as_of)
        g.series["coverage"] = self._coverage(counts, total)
        g.note = (
            "Funnel stages are cumulative — each includes every stage above "
            "it — so the bars decrease by construction. Coverage bars are "
            "independent siblings of one task and are not steps."
        )
        return g


# ---------------------------------------------------------------------------
# Drill-down
# ---------------------------------------------------------------------------


class StageDrilldown:
    """
    The document list behind a funnel drop or a coverage gap.

    Returns a real ``COUNT(*)`` alongside the page. The endpoint this replaces
    ran ``LIMIT 100`` and reported the length of the result as the count, so a
    tile reading "100" meant "at least 100" and no amount of paging could tell
    you the true figure.
    """

    #: Page size cap. A drill-down is for triage, not bulk export.
    MAX_PER_PAGE = 200

    def __init__(self, db: Session):
        self.db = db

    def fetch(self, stage_key: str, page: int = 1, per_page: int = 50) -> dict:
        stage = stages.get(stage_key)
        if stage is None:
            raise KeyError(stage_key)
        if stage.is_root:
            raise ValueError(
                f"'{stage_key}' is the funnel root — every document reaches it, "
                f"so it has no missing population to list."
            )

        page = max(1, page)
        per_page = max(1, min(per_page, self.MAX_PER_PAGE))

        criteria = stages.lost_at_criteria(stage)

        # load_only, not a bare query(Document): the model carries
        # extracted_text and a 1536-dimension vector, and a triage list needs
        # neither. Without it a 50-row page is megabytes.
        from sqlalchemy.orm import load_only

        base = self.db.query(Document)
        for c in criteria:
            base = base.filter(c)

        total = base.with_entities(func.count(Document.id)).scalar() or 0

        rows = (
            base.options(
                load_only(
                    Document.id,
                    Document.filename,
                    Document.status,
                    Document.created_at,
                    Document.processed_at,
                    Document.processing_error,
                )
            )
            .order_by(Document.created_at.desc().nullslast())
            .offset((page - 1) * per_page)
            .limit(per_page)
            .all()
        )

        return {
            "stage": {
                "key": stage.key,
                "label": stage.label,
                "kind": stage.kind,
                "missing_label": stage.missing_label,
                "reading": stage.reading,
            },
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": (total + per_page - 1) // per_page if per_page else 0,
            "documents": [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "status": d.status,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                    "processed_at": (
                        d.processed_at.isoformat() if d.processed_at else None
                    ),
                    "processing_error": d.processing_error,
                }
                for d in rows
            ],
            "as_of": scope.now_utc().isoformat(),
        }
