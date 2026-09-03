"""
The metric envelope.

Every metric in this package returns a ``Metric`` rather than a scalar. The
reason is the defect the dashboard audit actually found: the old panels were
not computing arithmetic wrongly, they were computing reasonable SQL over
populations nobody had written down. `queue_depth` counted a status with no
producer; `success_rate` filtered on a timestamp only success writes; four
"Missing X" tiles reported a LIMIT as a count. Each looked fine in isolation.

Carrying the denominator alongside the value makes that class of bug visible
at the point of authorship instead of at the point where someone notices two
cards disagree. ``as_dict()`` always emits ``denominator_label``, so a template
that renders a value has the label available in the same object — see
``templates/admin/_metric.html`` for the helper that renders them together.

``denominator_label`` is deliberately hand-written prose rather than derived
from the SQL predicate. "of 4,830 documents with a state" is not mechanically
recoverable from a filter expression, and an auto-generated label would drift
straight back into the vagueness this is meant to prevent. Writing it once per
metric forces the author to state the population in words.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

Number = Union[int, float]


@dataclass
class Metric:
    """
    One number plus everything needed to read it honestly.

    value
        The measurement itself.
    denominator
        The size of the population ``value`` was computed over, or None for a
        measure that is not a subset of anything (a duration, a rate already
        expressed as a percentage, a raw gauge).
    denominator_label
        Prose naming the population, rendered next to the value. Required.
    scope
        Which ``scope.py`` population was used. Free-form but should match a
        scope function name so the two can be cross-checked in review.
    as_of
        When the value was read. Must be timezone-aware — see scope.now_utc().
    caveat
        A known limitation that travels with the number wherever it is shown.
        Used where a metric is the best available answer but not a clean one,
        e.g. confidence levels that conflate machine scoring with human
        verification.
    """

    value: Optional[Number]
    denominator_label: str
    as_of: datetime
    denominator: Optional[int] = None
    scope: str = "corpus"
    caveat: Optional[str] = None

    def __post_init__(self):
        if not self.denominator_label or not self.denominator_label.strip():
            raise ValueError(
                "denominator_label is required — a metric may not be rendered "
                "without naming the population it was computed over."
            )
        if self.as_of.tzinfo is None:
            raise ValueError(
                "as_of must be timezone-aware; use services.metrics.scope.now_utc()."
            )

    @property
    def pct(self) -> Optional[float]:
        """
        ``value`` as a percentage of ``denominator``, or None when there is no
        denominator to divide by.

        A zero denominator returns None rather than 0.0 or 100.0: with nothing
        in the population the share is genuinely unknown, and the old dashboard
        defaulting an empty corpus to a green 100% success rate is exactly the
        reading this avoids.
        """
        if self.denominator is None or self.denominator == 0:
            return None
        if self.value is None:
            return None
        return self.value / self.denominator * 100

    def as_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "denominator": self.denominator,
            "denominator_label": self.denominator_label,
            "pct": round(self.pct, 2) if self.pct is not None else None,
            "scope": self.scope,
            "as_of": self.as_of.isoformat(),
            "caveat": self.caveat,
        }


@dataclass
class MetricGroup:
    """
    A named collection of metrics that share a population, plus optional
    free-form series data (chart points, table rows) that is not itself a
    single measurement.

    Groups exist so a panel can be serialised as one unit and so the shared
    denominator is stated once rather than repeated per metric. ``as_of`` is
    taken from the newest member so a cached response can report its own age.
    """

    name: str
    metrics: Dict[str, Metric] = field(default_factory=dict)
    series: Dict[str, List[Any]] = field(default_factory=dict)
    note: Optional[str] = None

    def add(self, key: str, metric: Metric) -> "MetricGroup":
        self.metrics[key] = metric
        return self

    @property
    def as_of(self) -> Optional[datetime]:
        if not self.metrics:
            return None
        return max(m.as_of for m in self.metrics.values())

    def as_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "metrics": {k: m.as_dict() for k, m in self.metrics.items()},
        }
        if self.series:
            out["series"] = self.series
        if self.note:
            out["note"] = self.note
        if self.as_of:
            out["as_of"] = self.as_of.isoformat()
        return out


# ---------------------------------------------------------------------------
# Verdict logic
#
# Zone 0 shows pairs of numbers whose *agreement* is the signal, not either
# figure alone: the Redis lease count against status='PROCESSING', and the
# broker queue depth against the database backlog. These helpers are pure
# functions so the thresholds are unit-testable without a database or a
# broker, which is most of what there is to get wrong here.
# ---------------------------------------------------------------------------

OK = "ok"
WARN = "warn"
BAD = "bad"
UNKNOWN = "unknown"


@dataclass
class Verdict:
    """A state plus the sentence explaining it. Rendered under a Zone 0 pair."""

    state: str
    detail: str

    def as_dict(self) -> Dict[str, str]:
        return {"state": self.state, "detail": self.detail}


def verdict_in_flight(lease_count: Optional[int], processing_count: int) -> Verdict:
    """
    Compare the Redis processing leases against rows marked PROCESSING.

    The lease is the more trustworthy of the two — it is held by a live task
    and expires on its own — so a surplus of PROCESSING rows means documents
    whose worker died, which is what the recovery daemon exists to sweep. The
    reverse surplus is rarer and worse: a task still working on a row something
    else has already moved on from.
    """
    if lease_count is None:
        return Verdict(
            UNKNOWN,
            f"{processing_count} marked processing; Redis unreachable, cannot cross-check",
        )
    if lease_count == processing_count:
        return Verdict(OK, "leases and status agree")
    if processing_count > lease_count:
        orphaned = processing_count - lease_count
        return Verdict(
            WARN if orphaned < 3 else BAD,
            f"{orphaned} marked processing with no live lease — likely abandoned",
        )
    return Verdict(
        BAD,
        f"{lease_count - processing_count} live lease(s) on documents not marked processing",
    )


def verdict_pending_work(broker_depth: Optional[int], db_backlog: int) -> Verdict:
    """
    Compare the Celery queue against the database backlog.

    ``broker_depth`` of None means the broker could not be read, which
    ``SchedulerService._broker_backlog`` deliberately distinguishes from zero.
    Reporting "unknown" matters: an empty queue means there is no work waiting,
    while an unreadable one means we have no idea, and conflating them is how
    you conclude the pipeline is idle while a backlog drains nowhere.
    """
    if broker_depth is None:
        return Verdict(
            UNKNOWN,
            f"{db_backlog} queued in database; broker unreadable, depth unknown",
        )
    if db_backlog == 0 and broker_depth == 0:
        return Verdict(OK, "nothing waiting")
    undispatched = db_backlog - broker_depth
    if undispatched <= 0:
        return Verdict(OK, "broker holds the backlog")
    return Verdict(
        WARN if undispatched < 10 else BAD,
        f"{undispatched} queued in database but absent from the broker — dispatch never landed",
    )


def verdict_zombies(zombie_count: int, threshold_seconds: int) -> Verdict:
    """Any zombie is a real one; the count only sets how loudly to say so."""
    if zombie_count == 0:
        return Verdict(OK, f"none stalled beyond {threshold_seconds}s")
    return Verdict(
        WARN if zombie_count < 5 else BAD,
        f"{zombie_count} stalled beyond {threshold_seconds}s awaiting recovery",
    )


def verdict_ingest_freshness(
    age_seconds: Optional[float], interval_seconds: int = 600
) -> Verdict:
    """
    Age of the Dropbox sync cursor against the cron interval.

    One missed run is tolerable and common — the cron and this read are not
    synchronised. Three missed runs is a stopped cron.
    """
    if age_seconds is None:
        return Verdict(UNKNOWN, "no sync cursor recorded — cron may never have run")
    if age_seconds <= interval_seconds:
        return Verdict(OK, "cron on schedule")
    if age_seconds <= interval_seconds * 3:
        return Verdict(WARN, "one or two runs missed")
    return Verdict(BAD, "cron appears stopped")


#: Queue wait thresholds, in seconds. Absolute rather than relative to the
#: task timeout: how long a document waits to be picked up is governed by
#: backlog and worker concurrency, and has no relationship to how long a task
#: is allowed to run once started.
QUEUE_WAIT_OK_SECONDS = 300
QUEUE_WAIT_WARN_SECONDS = 3600


def verdict_queue_wait(p95_seconds: Optional[float]) -> Verdict:
    """
    p95 time from upload to a worker picking the document up.

    Deliberately not scaled against the soft time limit. Queue wait is a
    backlog signal — the answer to a rising one is more concurrency, not
    faster processing — and measuring it against the task timeout would
    produce a confident, wrong claim that tasks are being killed.
    """
    if p95_seconds is None:
        return Verdict(UNKNOWN, "no documents have started processing")
    if p95_seconds <= QUEUE_WAIT_OK_SECONDS:
        return Verdict(OK, "documents start promptly")
    if p95_seconds <= QUEUE_WAIT_WARN_SECONDS:
        return Verdict(WARN, "backlog forming — documents waiting to start")
    return Verdict(
        BAD, "documents waiting over an hour to start — worker capacity is the limit"
    )


def verdict_duration(p95_seconds: Optional[float], soft_limit_seconds: int) -> Verdict:
    """
    p95 worker duration against the Celery soft time limit.

    The limit truncates the distribution — a task past it is killed and marked
    FAILED — so a p95 approaching it means documents are being lost to the
    timeout rather than merely running slowly.
    """
    if p95_seconds is None:
        return Verdict(UNKNOWN, "no completed documents with timing data")
    share = p95_seconds / soft_limit_seconds * 100
    if share < 50:
        return Verdict(OK, f"p95 is {share:.0f}% of the {soft_limit_seconds}s limit")
    if share < 85:
        return Verdict(WARN, f"p95 is {share:.0f}% of the {soft_limit_seconds}s limit")
    return Verdict(
        BAD, f"p95 is {share:.0f}% of the {soft_limit_seconds}s limit — tasks are being killed"
    )
