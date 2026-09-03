"""
Population definitions — the single home for "which documents are we talking
about".

Every metric module takes its population from a function here and nowhere
else. The rule is worth stating plainly because breaking it is what produced
the original defect: no module under services/metrics/ may filter on
``Document.status`` directly.

The old dashboard had thirteen metric methods each choosing its own filter,
which is how one card counted COMPLETED only, another counted every row
including QUEUED and FAILED, and both were labelled "total". With the choices
named and centralised, a reviewer can see which population a metric used by
reading one word, and changing what "the corpus" means is one edit rather than
thirteen.

Also the only clock. ``Document`` timestamps are ``timestamptz``; the previous
service compared them against naive ``datetime.utcnow()`` in five places,
which raised TypeError in one of them and silently depended on the session
time zone in the rest.
"""

from datetime import datetime, timedelta, timezone
from typing import Sequence

from sqlalchemy import func
from sqlalchemy.orm import Query

from config import get_settings
from models.document import Document, DocumentStatus

settings = get_settings()


# ---------------------------------------------------------------------------
# Status sets
#
# PENDING is folded into BACKLOG rather than dropped. It has no producer left
# (see models/document.py) so nothing new arrives in it, but historical rows
# are real work that the recovery daemon still sweeps back to QUEUED — and the
# old dashboard's queue_depth counted PENDING *alone*, which is why it read
# zero while QUEUED documents piled up behind it.
# ---------------------------------------------------------------------------

TERMINAL: Sequence[str] = (DocumentStatus.COMPLETED, DocumentStatus.FAILED)
BACKLOG: Sequence[str] = (DocumentStatus.QUEUED, DocumentStatus.PENDING)
ACTIVE: Sequence[str] = (
    DocumentStatus.QUEUED,
    DocumentStatus.PENDING,
    DocumentStatus.PROCESSING,
)

#: Every status a row can currently hold, for a breakdown that has to sum to
#: the corpus. PENDING is included so historical rows are not silently
#: dropped from a chart that claims to show everything.
ALL_STATUSES: Sequence[str] = (
    DocumentStatus.QUEUED,
    DocumentStatus.PENDING,
    DocumentStatus.PROCESSING,
    DocumentStatus.COMPLETED,
    DocumentStatus.FAILED,
)


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------


def now_utc() -> datetime:
    """
    The only clock in the metrics layer.

    Timezone-aware, matching the ``timestamptz`` columns and matching
    ``scheduler_service.py``, which already does this correctly. Never
    ``datetime.utcnow()``.
    """
    return datetime.now(timezone.utc)


def ago(**kwargs) -> datetime:
    """
    A timezone-aware cutoff, e.g. ``ago(days=7)``.

    Exists so no metric writes its own ``now_utc() - timedelta(...)`` and
    accidentally reintroduces a naive value on one of the two sides.
    """
    return now_utc() - timedelta(**kwargs)


def ago_naive(**kwargs) -> datetime:
    """
    A NAIVE cutoff, for the one naive column in the schema.

    ``search_queries.timestamp`` is ``DateTime`` without a timezone and is
    written from Python's ``utcnow()``. Comparing it against an aware cutoff
    makes Postgres coerce the column using the session TimeZone, so the same
    query would slice differently depending on server configuration.

    This is the only sanctioned naive value in the metrics layer, and it
    exists solely to match that column. Everything touching ``documents``
    uses :func:`ago`.
    """
    return (now_utc() - timedelta(**kwargs)).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Thresholds derived from settings
#
# Read through, never retyped. `zombie_threshold_seconds` is a computed
# property on Settings (task_time_limit + zombie_grace_seconds, itself
# processing_timeout + processing_timeout_grace + zombie_grace_seconds) and is
# the same value scheduler_service.py uses. If the dashboard hardcoded it, the
# dashboard and the recovery daemon could disagree about what "stuck" means.
# ---------------------------------------------------------------------------


def zombie_threshold_seconds() -> int:
    """Seconds of silence after which a PROCESSING document is abandoned."""
    return settings.zombie_threshold_seconds


def soft_time_limit_seconds() -> int:
    """The Celery soft limit a task is killed at, for scaling duration charts."""
    return settings.processing_timeout


def zombie_cutoff() -> datetime:
    """The instant before which a heartbeat is considered stale."""
    return now_utc() - timedelta(seconds=zombie_threshold_seconds())


# ---------------------------------------------------------------------------
# Populations
#
# Each takes and returns a Query so they compose: completed(with_cost(q)).
# ---------------------------------------------------------------------------


def corpus(q: Query) -> Query:
    """
    Every row. The only honest "total".

    Used as the denominator for the pipeline funnel and for any panel whose
    missing mass is part of the story.
    """
    return q


def terminal(q: Query) -> Query:
    """
    Documents whose processing has finished, successfully or not.

    The correct denominator for a success rate. The old dashboard filtered on
    ``processed_at >= cutoff`` instead, and because ``processed_at`` is written
    only on the COMPLETED branch that predicate excluded every failure — which
    pinned the reported success rate to exactly 100% regardless of reality.
    """
    return q.filter(Document.status.in_(TERMINAL))


def completed(q: Query) -> Query:
    """Successfully processed documents."""
    return q.filter(Document.status == DocumentStatus.COMPLETED)


def failed(q: Query) -> Query:
    """Documents currently in a failed state."""
    return q.filter(Document.status == DocumentStatus.FAILED)


def processing(q: Query) -> Query:
    """Documents a worker claims to be working on right now."""
    return q.filter(Document.status == DocumentStatus.PROCESSING)


def backlog(q: Query) -> Query:
    """
    Work waiting to start — QUEUED plus the vestigial PENDING.

    This is what "queue depth" means. See the note on the status sets above
    for why PENDING is in here.
    """
    return q.filter(Document.status.in_(BACKLOG))


def active(q: Query) -> Query:
    """Anything not yet finished: waiting or in progress."""
    return q.filter(Document.status.in_(ACTIVE))


def extracted(q: Query) -> Query:
    """
    Documents the feature-extraction task has produced a client for.

    A proxy for "the political metadata block has been populated", and the
    correct denominator for the client, state, date, and franking panels. It
    is a proxy rather than a certainty: the task can resolve no client name
    from a document it did in fact process. It errs toward under-counting
    coverage, which is the safe direction for a denominator.
    """
    return q.filter(Document.client_canonical.isnot(None))


def with_timings(q: Query) -> Query:
    """
    Completed documents that can yield a true worker duration.

    Requires both ends of the measurement. There is deliberately no fallback
    to ``created_at``: that would silently fold queue wait into processing
    time, and the two are separately meaningful. Legacy rows without
    ``processing_started_at`` are excluded and counted separately so the
    exclusion is visible rather than assumed.
    """
    return q.filter(
        Document.status == DocumentStatus.COMPLETED,
        Document.processed_at.isnot(None),
        Document.processing_started_at.isnot(None),
    )


def zombies(q: Query) -> Query:
    """
    PROCESSING documents that have shown no sign of life past the threshold.

    Mirrors ``SchedulerService._rescue_zombie_documents`` exactly, including
    the NULL-heartbeat fallback to ``processing_started_at`` for rows that
    predate the heartbeat column. Kept in step deliberately: the dashboard
    should report precisely the set the daemon will act on, so a non-zero
    count here means recovery has not caught up rather than that the two
    disagree about the definition.
    """
    from sqlalchemy import and_, or_

    cutoff = zombie_cutoff()
    return q.filter(
        Document.status == DocumentStatus.PROCESSING,
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


# ---------------------------------------------------------------------------
# Time bucketing
# ---------------------------------------------------------------------------


def resolution_time():
    """
    The column expression for "when did this document reach a terminal state".

    ``processed_at`` is written only on success, so failures have no completion
    timestamp at all — there is no column recording when a document failed.
    Failures do move ``updated_at`` when their status changes, so this
    coalesces the two and uses the result purely as a *time bucket*.

    The rate itself is always computed from ``status`` (see ``terminal``), so a
    mis-bucketed failure can shift a point along the trend line but can never
    change the rate. That separation is the whole reason this is safe to use
    without a schema change: the imprecise value never touches the numerator or
    the denominator, only the x-axis.
    """
    return func.coalesce(Document.processed_at, Document.updated_at)


def corpus_total(db) -> int:
    """
    ``COUNT(*)`` of documents, memoised for the life of one Session.

    Seven collectors each need the corpus size as a denominator, and each was
    issuing its own full count — seven scans of the same table for one number
    that cannot change within a request. The memo lives on ``Session.info``,
    so it is scoped to the request and disappears with it.
    """
    cached = db.info.get("_metrics_corpus_total")
    if cached is None:
        cached = count(corpus(db.query(Document)))
        db.info["_metrics_corpus_total"] = cached
    return cached


def count(q: Query) -> int:
    """
    Row count for a scoped query.

    Always a real ``COUNT(*)``. The old incomplete-documents endpoint ran four
    ``LIMIT 100`` queries and reported ``len()`` of each as a count, so a tile
    reading "100" meant "at least 100" and the dashboard contradicted its own
    review panel on the same screen.
    """
    return q.with_entities(func.count(Document.id)).scalar() or 0
