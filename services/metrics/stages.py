"""
The stage registry — one definition per pipeline stage, used by both the
funnel counts and the drill-down lists.

This module exists to prevent a specific regression. The old dashboard
computed "missing embeddings" twice: once in ``get_incomplete_documents``
(over COMPLETED+FAILED, capped at 100 rows, reporting the cap as a count) and
once in ``get_review_queue`` (over COMPLETED only, uncapped). Both rendered on
the same screen, and they disagreed. Two implementations of one idea will
always drift; there is only one here, and the funnel and the drill-down both
read it.

Two kinds of stage, deliberately not drawn the same way:

``funnel``
    Sequential steps of the processing pipeline. Each stage's *reached*
    predicate is cumulative — it ANDs every prior stage — so the funnel is a
    true funnel and the counts are monotonically decreasing by construction
    rather than by hope.

``coverage``
    Independent outputs of the feature-extraction task. A document can have a
    state and no date; these are siblings, not gates, and are shown as
    separate bars. Drawing them as funnel steps would imply an ordering that
    does not exist.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from sqlalchemy import Text, and_, cast, func, not_

from models.document import Document
from services.metrics.jsonb import array_length, has_key

#: The literal an AI analysis carries when it completed but produced nothing
#: usable. Present in ai_analysis with a non-null summary, so a NULL check
#: alone reports these documents as successful.
PLACEHOLDER_SUMMARY = "%no summary available%"


# ---------------------------------------------------------------------------
# Predicates
#
# Each returns a SQLAlchemy criterion rather than transforming a Query, so
# they can be ANDed together to build the cumulative funnel predicates.
# ---------------------------------------------------------------------------


def _has_text():
    """
    Has non-empty extracted text.

    ``substr(col, 1, 1) <> ''`` rather than ``col <> ''``. The two are
    identical for a non-null value — a string is empty exactly when its first
    character is — but comparing the column directly detoasts the whole value,
    and this column holds the full OCR output of a multi-page document. Asking
    for one character lets Postgres stop after decompressing a prefix.

    This predicate gates every later funnel stage, so it is evaluated for every
    row on every dashboard load.
    """
    return and_(
        Document.extracted_text.isnot(None),
        func.substr(Document.extracted_text, 1, 1) != "",
    )


def _has_analysis():
    return Document.ai_analysis.isnot(None)


def _has_real_summary():
    """
    A summary that is actually a summary.

    Three ways this field fails, and the old dashboard's NULL check caught
    only the first: the key is absent, the key is an empty string, or the
    analysis "succeeded" and wrote the placeholder. The last is the dangerous
    one — the document reads as COMPLETED with an ai_analysis present.
    """
    summary = Document.ai_analysis["summary"].astext
    return and_(
        summary.isnot(None),
        summary != "",
        not_(summary.ilike(PLACEHOLDER_SUMMARY)),
    )


def _has_mappings():
    # Via services.metrics.jsonb: the deployed keywords column is json, not
    # jsonb, and jsonb_array_length has no json overload.
    return array_length(Document.keywords, "keyword_mappings") > 0


def _has_embedding():
    return Document.search_vector.isnot(None)


def _feature_task_ran():
    """
    Whether the feature-extraction task has processed this document at all.

    Reads the ``feature_extraction`` key the worker writes into file_metadata
    on completion, rather than inferring it from client_canonical being
    non-null. The distinction matters: the task can run and legitimately
    resolve no client name, and conflating "never ran" with "ran and found
    nothing" is what makes extraction coverage unreadable. The gap between
    this bar and the client bar is exactly the documents in the second case.
    """
    return has_key(Document.file_metadata, "feature_extraction")


def _has_client():
    return and_(
        Document.client_canonical.isnot(None),
        Document.client_canonical != "",
    )


def _has_state():
    """
    TRIM is not optional — untrimmed values exist in this column, which is why
    every read path in the codebase strips it. Without it a naive count treats
    ``'MI '`` and ``'MI'`` as different states.
    """
    return and_(
        Document.state.isnot(None),
        func.trim(cast(Document.state, Text)) != "",
    )


def _has_date():
    return Document.date_created.isnot(None)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Stage:
    """
    One stage, its predicate, and the prose describing what its absence means.

    ``missing_label`` is written for the drill-down header, so a reader
    arriving at a list of documents knows what they are looking at without
    reverse-engineering the predicate.
    """

    key: str
    label: str
    kind: str  # "funnel" | "coverage"
    predicate: Optional[Callable]  # None for the always-true root stage
    missing_label: str
    reading: str  # what a drop here means

    @property
    def is_root(self) -> bool:
        return self.predicate is None


FUNNEL_STAGES: List[Stage] = [
    Stage(
        key="uploaded",
        label="Uploaded",
        kind="funnel",
        predicate=None,
        missing_label="—",
        reading="Every row. The denominator for everything below.",
    ),
    Stage(
        key="extracted_text",
        label="Extracted text",
        kind="funnel",
        predicate=_has_text,
        missing_label="uploaded but no text extracted",
        reading="OCR or extraction produced nothing, or the document has not been processed yet.",
    ),
    Stage(
        key="ai_analysis",
        label="AI analysis",
        kind="funnel",
        predicate=_has_analysis,
        missing_label="has text but no AI analysis",
        reading="Text was extracted but analysis never ran or never stored a result.",
    ),
    Stage(
        key="real_summary",
        label="Real summary",
        kind="funnel",
        predicate=_has_real_summary,
        missing_label="has analysis but no usable summary",
        reading=(
            "Silent AI failure: analysis completed and wrote a placeholder. "
            "These documents look successful by status and by ai_analysis "
            "being non-null."
        ),
    ),
    Stage(
        key="keyword_mappings",
        label="Keyword mappings",
        kind="funnel",
        predicate=_has_mappings,
        missing_label="summarised but no taxonomy mappings",
        reading="Taxonomy mapping produced no terms, so the document is invisible to topic filters.",
    ),
    Stage(
        key="embedding",
        label="Embedding",
        kind="funnel",
        predicate=_has_embedding,
        missing_label="mapped but not embedded",
        reading=(
            "Written by the feature task, not the AI task, so a lag here is "
            "expected rather than a failure — but semantic search silently "
            "misses these documents until it clears."
        ),
    ),
]


COVERAGE_STAGES: List[Stage] = [
    Stage(
        key="feature_task_ran",
        label="Feature task ran",
        kind="coverage",
        predicate=_feature_task_ran,
        missing_label="never processed by the feature task",
        reading="The upstream gate for every field below it.",
    ),
    Stage(
        key="client",
        label="Client",
        kind="coverage",
        predicate=_has_client,
        missing_label="no client resolved",
        reading="Denominator for the client, normalisation, and franking panels.",
    ),
    Stage(
        key="state",
        label="State",
        kind="coverage",
        predicate=_has_state,
        missing_label="no state resolved",
        reading="Denominator for the geography panel.",
    ),
    Stage(
        key="date",
        label="Document date",
        kind="coverage",
        predicate=_has_date,
        missing_label="no document date resolved",
        reading=(
            "Denominator for the timeline. Dates outside 2019-2026 were "
            "discarded to NULL at write time, so this excludes range "
            "violations as well as absences."
        ),
    ),
]


ALL_STAGES: Dict[str, Stage] = {
    s.key: s for s in (*FUNNEL_STAGES, *COVERAGE_STAGES)
}


def get(key: str) -> Optional[Stage]:
    return ALL_STAGES.get(key)


# ---------------------------------------------------------------------------
# Predicate composition
# ---------------------------------------------------------------------------


def reached_criteria(stage: Stage) -> List:
    """
    Criteria for "this document reached ``stage``".

    For a funnel stage this is cumulative: every prior stage's predicate is
    included, so "reached embedding" means "has text and analysis and a real
    summary and mappings and an embedding". That is what makes the funnel
    monotonic by construction — a later stage cannot out-count an earlier one.

    For a coverage stage it is the single predicate, because coverage stages
    are siblings rather than steps.
    """
    if stage.kind == "coverage":
        return [] if stage.is_root else [stage.predicate()]

    criteria = []
    for s in FUNNEL_STAGES:
        if not s.is_root:
            criteria.append(s.predicate())
        if s.key == stage.key:
            break
    return criteria


def independent_criteria(stage: Stage) -> List:
    """
    Criteria for "this document has this stage's artifact", ignoring order.

    Reported alongside the cumulative count so the two can be compared. An
    independent count higher than the cumulative one means documents hold a
    later artifact while missing an earlier one — an embedding with no
    extracted text, say. That is a real corruption signal and it is invisible
    if only one of the two numbers is computed.
    """
    return [] if stage.is_root else [stage.predicate()]


def lost_at_criteria(stage: Stage) -> List:
    """
    Criteria for "this document was lost at ``stage``" — the drill-down set.

    Reached every prior stage, but not this one. This is the actionable
    population: it is precisely the drop the funnel draws, so the list length
    and the bar's delta are the same number by construction rather than by two
    queries happening to agree.

    For a coverage stage there is no prior, so this is simply "does not have
    it".
    """
    if stage.is_root:
        return []

    if stage.kind == "coverage":
        return [not_(stage.predicate())]

    prior = []
    for s in FUNNEL_STAGES:
        if s.key == stage.key:
            break
        if not s.is_root:
            prior.append(s.predicate())
    return [*prior, not_(stage.predicate())]
