"""
Tests for the metrics foundation — the Metric envelope, the scope module, and
the Zone 0 verdict logic.

These are pure-logic tests. The Document model uses Postgres-only column types
(JSONB, TSVECTOR via a raw to_tsvector Computed column, pgvector's Vector) so
it cannot be created against SQLite, and no Postgres instance is available in
this environment — the same constraint documented in test_worker_recovery.py.

That shaped the code under test rather than just the tests: the parts of the
metrics layer most likely to be wrong are the threshold comparisons and the
pair-divergence rules, so those live in ``envelope.py`` as pure functions that
need neither a database nor a broker. What is *not* covered here is whether
the SQL predicates in ``scope.py`` match real rows; that needs an integration
test against Postgres, or the profiler script run against production.
"""

from datetime import datetime, timedelta, timezone

import pytest

from services.metrics.envelope import (
    BAD,
    OK,
    UNKNOWN,
    WARN,
    Metric,
    MetricGroup,
    verdict_duration,
    verdict_in_flight,
    verdict_ingest_freshness,
    verdict_pending_work,
    verdict_queue_wait,
    verdict_zombies,
)
from services.metrics import scope


def aware(**kw) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kw)


# ---------------------------------------------------------------------------
# The envelope
# ---------------------------------------------------------------------------


class TestMetricEnvelope:
    def test_requires_a_denominator_label(self):
        """
        The whole point of the envelope: a number cannot exist in this layer
        without prose naming the population it came from.
        """
        with pytest.raises(ValueError, match="denominator_label is required"):
            Metric(value=5, denominator_label="", as_of=aware())

    def test_rejects_whitespace_only_label(self):
        with pytest.raises(ValueError, match="denominator_label is required"):
            Metric(value=5, denominator_label="   ", as_of=aware())

    def test_rejects_naive_as_of(self):
        """
        Naive datetimes are what broke the old service against timestamptz
        columns. The envelope refuses them at construction so a naive value
        cannot propagate into a comparison later.
        """
        with pytest.raises(ValueError, match="timezone-aware"):
            Metric(
                value=1,
                denominator_label="of everything",
                as_of=datetime.utcnow(),  # deliberately naive
            )

    def test_pct_computes_share(self):
        m = Metric(
            value=25, denominator=200, denominator_label="of everything", as_of=aware()
        )
        assert m.pct == 12.5

    def test_pct_is_none_without_denominator(self):
        """A duration is not a share of anything."""
        m = Metric(value=14.2, denominator_label="seconds", as_of=aware())
        assert m.pct is None

    def test_zero_denominator_yields_none_not_a_full_bar(self):
        """
        With nothing in the population the share is unknown, not 0% and
        emphatically not 100%. The old dashboard returned a green 100% success
        rate for an empty corpus, which is the reading this prevents.
        """
        m = Metric(
            value=0, denominator=0, denominator_label="of documents that finished",
            as_of=aware(),
        )
        assert m.pct is None

    def test_none_value_yields_none_pct(self):
        """An unreachable gauge has no share either."""
        m = Metric(
            value=None, denominator=100, denominator_label="of everything", as_of=aware()
        )
        assert m.pct is None

    def test_as_dict_always_carries_the_label_and_caveat(self):
        m = Metric(
            value=3,
            denominator=10,
            denominator_label="of documents that finished processing",
            as_of=aware(),
            scope="terminal",
            caveat="never cleared",
        )
        d = m.as_dict()
        assert d["denominator_label"] == "of documents that finished processing"
        assert d["caveat"] == "never cleared"
        assert d["pct"] == 30.0
        assert d["scope"] == "terminal"
        # as_of must survive as an ISO string with an offset, so a cached
        # payload can report its true age.
        assert d["as_of"].endswith("+00:00")


class TestMetricGroup:
    def test_as_of_is_the_newest_member(self):
        g = MetricGroup(name="z")
        old, new = aware(minutes=10), aware(minutes=1)
        g.add("a", Metric(value=1, denominator_label="x", as_of=old))
        g.add("b", Metric(value=2, denominator_label="x", as_of=new))
        assert g.as_of == new

    def test_empty_group_has_no_as_of(self):
        assert MetricGroup(name="z").as_of is None

    def test_as_dict_omits_absent_optional_sections(self):
        g = MetricGroup(name="z")
        g.add("a", Metric(value=1, denominator_label="x", as_of=aware()))
        d = g.as_dict()
        assert "series" not in d and "note" not in d
        assert d["metrics"]["a"]["value"] == 1


# ---------------------------------------------------------------------------
# Zone 0 verdicts — the pair-divergence rules
# ---------------------------------------------------------------------------


class TestInFlightVerdict:
    def test_agreement_is_healthy(self):
        assert verdict_in_flight(2, 2).state == OK

    def test_unreadable_redis_is_unknown_not_zero(self):
        """
        A missing lease count must not read as "no work in flight" — that is
        the same conflation of unknown and empty that the broker check guards.
        """
        v = verdict_in_flight(None, 3)
        assert v.state == UNKNOWN
        assert "cannot cross-check" in v.detail

    def test_surplus_processing_rows_are_abandoned_documents(self):
        """
        The lease is the trustworthy side: it is held by a live task and
        expires on its own. More PROCESSING rows than leases means workers
        died, which is precisely what the recovery daemon sweeps.
        """
        v = verdict_in_flight(1, 3)
        assert v.state == WARN
        assert "2 marked processing with no live lease" in v.detail

    def test_large_surplus_escalates(self):
        assert verdict_in_flight(0, 9).state == BAD

    def test_surplus_leases_is_the_worse_direction(self):
        """
        A live lease on a document nothing considers in-progress means a task
        is writing to a row another actor has already moved on from. Rarer
        than the reverse, and worse, so it escalates immediately.
        """
        assert verdict_in_flight(3, 1).state == BAD


class TestPendingWorkVerdict:
    def test_unreadable_broker_is_unknown_not_empty(self):
        """
        SchedulerService._broker_backlog deliberately distinguishes None from
        0. Reporting an unreadable broker as an empty queue is how you
        conclude the pipeline is idle while a backlog drains nowhere.
        """
        v = verdict_pending_work(None, 40)
        assert v.state == UNKNOWN
        assert "depth unknown" in v.detail

    def test_both_empty_is_healthy(self):
        assert verdict_pending_work(0, 0).state == OK

    def test_broker_holding_the_backlog_is_healthy(self):
        assert verdict_pending_work(12, 12).state == OK

    def test_broker_ahead_of_db_is_not_an_alarm(self):
        """
        A task already claimed but whose row has moved to PROCESSING is normal
        in-flight churn, not a fault.
        """
        assert verdict_pending_work(14, 12).state == OK

    def test_db_surplus_means_dispatch_never_landed(self):
        v = verdict_pending_work(12, 47)
        assert v.state == BAD
        assert "35 queued in database but absent from the broker" in v.detail

    def test_small_db_surplus_only_warns(self):
        assert verdict_pending_work(10, 13).state == WARN


class TestZombieVerdict:
    def test_none_is_healthy_and_names_the_threshold(self):
        v = verdict_zombies(0, 2040)
        assert v.state == OK
        assert "2040s" in v.detail

    def test_any_zombie_is_reported(self):
        assert verdict_zombies(1, 2040).state == WARN

    def test_many_zombies_escalate(self):
        assert verdict_zombies(12, 2040).state == BAD


class TestIngestFreshnessVerdict:
    def test_within_the_cron_interval_is_healthy(self):
        assert verdict_ingest_freshness(240, 600).state == OK

    def test_a_missed_run_warns(self):
        assert verdict_ingest_freshness(1200, 600).state == WARN

    def test_three_missed_runs_is_a_stopped_cron(self):
        assert verdict_ingest_freshness(5000, 600).state == BAD

    def test_no_cursor_is_unknown(self):
        v = verdict_ingest_freshness(None, 600)
        assert v.state == UNKNOWN
        assert "may never have run" in v.detail


class TestDurationVerdict:
    def test_comfortably_under_the_limit(self):
        v = verdict_duration(412, 1800)
        assert v.state == OK
        assert "23%" in v.detail

    def test_approaching_the_limit_warns(self):
        assert verdict_duration(1200, 1800).state == WARN

    def test_at_the_limit_means_tasks_are_being_killed(self):
        """
        The soft limit truncates the distribution: a task past it is killed and
        the document marked FAILED. A p95 at the limit means documents are
        being lost to the timeout, not merely running slowly.
        """
        v = verdict_duration(1750, 1800)
        assert v.state == BAD
        assert "being killed" in v.detail

    def test_no_timing_data_is_unknown(self):
        assert verdict_duration(None, 1800).state == UNKNOWN


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestScopeStatusSets:
    def test_backlog_includes_vestigial_pending(self):
        """
        PENDING has no producer left, but historical rows are real waiting
        work. The old queue_depth counted PENDING *alone*, so it read zero
        while QUEUED documents piled up behind it — the inverse of this bug.
        """
        assert "QUEUED" in scope.BACKLOG
        assert "PENDING" in scope.BACKLOG

    def test_terminal_is_exactly_the_finished_states(self):
        assert set(scope.TERMINAL) == {"COMPLETED", "FAILED"}

    def test_all_statuses_covers_every_state_a_breakdown_must_sum_over(self):
        """
        A status breakdown has to sum to the corpus. The old doughnut omitted
        QUEUED entirely, so its slices silently did not add up.
        """
        assert set(scope.ALL_STATUSES) == {
            "QUEUED",
            "PENDING",
            "PROCESSING",
            "COMPLETED",
            "FAILED",
        }

    def test_active_and_terminal_partition_all_statuses(self):
        assert set(scope.ACTIVE) | set(scope.TERMINAL) == set(scope.ALL_STATUSES)
        assert not set(scope.ACTIVE) & set(scope.TERMINAL)


class TestScopeClock:
    def test_now_utc_is_aware(self):
        assert scope.now_utc().tzinfo is not None

    def test_ago_is_aware_and_in_the_past(self):
        cutoff = scope.ago(days=7)
        assert cutoff.tzinfo is not None
        assert cutoff < scope.now_utc()

    def test_ago_result_is_comparable_to_now(self):
        """
        The regression guard: naive vs aware subtraction is what raised
        TypeError in the old get_queue_health_data.
        """
        assert (scope.now_utc() - scope.ago(hours=1)).total_seconds() == pytest.approx(
            3600, abs=5
        )


class TestScopeThresholds:
    def test_zombie_threshold_matches_the_scheduler(self):
        """
        The dashboard and the recovery daemon must not disagree about what
        "stuck" means. Both read the same computed setting rather than a
        literal.
        """
        from services.scheduler_service import ZOMBIE_THRESHOLD_SECONDS

        assert scope.zombie_threshold_seconds() == ZOMBIE_THRESHOLD_SECONDS

    def test_zombie_threshold_is_derived_from_the_enforced_task_limit(self):
        from config import get_settings

        s = get_settings()
        assert scope.zombie_threshold_seconds() == (
            s.processing_timeout + s.processing_timeout_grace + s.zombie_grace_seconds
        )

    def test_zombie_cutoff_is_aware_and_the_threshold_in_the_past(self):
        cutoff = scope.zombie_cutoff()
        assert cutoff.tzinfo is not None
        age = (scope.now_utc() - cutoff).total_seconds()
        assert age == pytest.approx(scope.zombie_threshold_seconds(), abs=5)

    def test_soft_limit_is_the_celery_timeout(self):
        from config import get_settings

        assert scope.soft_time_limit_seconds() == get_settings().processing_timeout


class TestQueueWaitVerdict:
    """
    Queue wait must not be judged against the task soft-timeout.

    An earlier draft passed queue wait into verdict_duration, which scales
    against processing_timeout. A p95 wait of 1.4h then rendered as "280% of
    the 1800s limit — tasks are being killed": confident, alarming, and false.
    Queue wait is a backlog signal and has no relationship to how long a task
    may run once it has started.
    """

    def test_prompt_start_is_healthy(self):
        assert verdict_queue_wait(45).state == OK

    def test_backlog_forming_warns(self):
        v = verdict_queue_wait(900)
        assert v.state == WARN
        assert "backlog forming" in v.detail

    def test_over_an_hour_is_a_capacity_problem(self):
        v = verdict_queue_wait(5040)
        assert v.state == BAD
        assert "worker capacity" in v.detail

    def test_no_data_is_unknown(self):
        assert verdict_queue_wait(None).state == UNKNOWN

    def test_never_claims_tasks_are_being_killed(self):
        """
        The specific wrong message from the earlier draft. A long queue wait
        says nothing about the soft limit.
        """
        for wait in (900, 5040, 86400):
            assert "killed" not in verdict_queue_wait(wait).detail


class TestIngestCursorFailureIsContained:
    """
    A missing dropbox_sync_state table must degrade one tile, not the endpoint.
    """

    def test_missing_table_rolls_back_the_transaction(self):
        """
        Postgres aborts the whole transaction on a failed statement. Without a
        rollback here, every subsequent query in collect() raises
        InFailedSqlTransaction — so the defensive branch would convert a
        missing tile into a 500 for all of Zone 0.
        """
        from unittest.mock import MagicMock

        from services.metrics.now import NowMetrics

        db = MagicMock()
        db.execute.side_effect = Exception('relation "dropbox_sync_state" does not exist')

        svc = NowMetrics(db)
        assert svc._ingest_cursor_age_seconds() is None
        db.rollback.assert_called_once()

    def test_absent_row_needs_no_rollback(self):
        """A successful query returning nothing is not an error."""
        from unittest.mock import MagicMock

        from services.metrics.now import NowMetrics

        db = MagicMock()
        db.execute.return_value.fetchone.return_value = None

        svc = NowMetrics(db)
        assert svc._ingest_cursor_age_seconds() is None
        db.rollback.assert_not_called()

    def test_reads_the_age(self):
        from unittest.mock import MagicMock

        from services.metrics.now import NowMetrics

        db = MagicMock()
        db.execute.return_value.fetchone.return_value = (243.5,)

        assert NowMetrics(db)._ingest_cursor_age_seconds() == 243.5
