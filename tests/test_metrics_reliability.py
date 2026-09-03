"""
Tests for Zone 2 assembly — chiefly the regression that a failure lowers the
success rate.

The old dashboard's success rate could not do that. It filtered both numerator
and denominator on ``processed_at >= cutoff``, and ``processed_at`` is written
only on the COMPLETED branch, so the two queries were identical and the rate
was 100.00% by construction. That is the specific behaviour these tests pin.

Population counting is stubbed rather than run against a database: the Document
model uses Postgres-only column types and no Postgres is available here (see
test_worker_recovery.py for the same constraint). Stubbing ``scope.count``
exercises the real assembly arithmetic and the real envelope construction,
which is where the reasoning lives. It does not verify that the scope
predicates select the right rows — that needs Postgres, or the profiler run
against production.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.metrics.reliability import ReliabilityMetrics


def build(total, terminal, completed, failed, ever_failed, recovered):
    """
    Run collect() with population counts stubbed.

    The side_effect order mirrors the sequence of scope.count() calls in
    ReliabilityMetrics.collect: total, terminal, completed, failed,
    ever_failed, recovered. Asserted below via call_count so a reordering in
    collect() fails loudly here rather than silently scrambling the values.
    """
    svc = ReliabilityMetrics(MagicMock())
    counts = [total, terminal, completed, failed, ever_failed, recovered]

    with patch(
        "services.metrics.reliability.scope.count", side_effect=counts
    ) as counter, patch.object(
        ReliabilityMetrics, "_outcome_trend", return_value=[]
    ), patch.object(
        ReliabilityMetrics, "_recent_failures", return_value=[]
    ):
        group = svc.collect()
        assert counter.call_count == len(counts), (
            "collect() changed how many populations it counts; update the "
            "side_effect order in this helper to match."
        )
    return group


class TestSuccessRateRegression:
    def test_failures_lower_the_rate(self):
        """
        The headline regression. 900 completed of 1000 finished is 90%, not
        the 100% the old implementation reported for any input whatsoever.
        """
        g = build(
            total=1200, terminal=1000, completed=900, failed=100,
            ever_failed=140, recovered=40,
        )
        assert g.metrics["success_rate"].value == 90.0

    def test_all_failures_reads_zero_not_one_hundred(self):
        g = build(
            total=50, terminal=50, completed=0, failed=50,
            ever_failed=50, recovered=0,
        )
        assert g.metrics["success_rate"].value == 0.0

    def test_rate_denominator_is_terminal_not_corpus(self):
        """
        Documents still queued must not count against the success rate — they
        have not failed, they have not finished. The denominator is
        'documents that finished processing', and the label says so.
        """
        g = build(
            total=5000, terminal=1000, completed=900, failed=100,
            ever_failed=140, recovered=40,
        )
        m = g.metrics["success_rate"]
        assert m.denominator == 1000
        assert m.scope == "terminal"
        assert m.denominator_label == "of documents that finished processing"

    def test_empty_corpus_is_unknown_not_perfect(self):
        """
        With nothing finished the rate is unknown. The old implementation
        returned a green 100.0 for an empty corpus.
        """
        g = build(0, 0, 0, 0, 0, 0)
        assert g.metrics["success_rate"].value is None
        assert g.metrics["success_rate"].pct is None


class TestEverFailedVersusCurrentlyFailed:
    def test_they_are_separate_metrics(self):
        """
        processing_error is never cleared on a later success, so 'has ever
        recorded an error' is strictly larger than 'is failed now'. The old
        review panel showed only the former under the label 'Processing
        Errors', implying documents were broken that had since recovered.
        """
        g = build(
            total=1200, terminal=1000, completed=900, failed=100,
            ever_failed=140, recovered=40,
        )
        assert g.metrics["currently_failed"].value == 100
        assert g.metrics["ever_failed"].value == 140
        assert g.metrics["recovered"].value == 40

    def test_ever_failed_carries_its_caveat(self):
        g = build(1200, 1000, 900, 100, 140, 40)
        caveat = g.metrics["ever_failed"].caveat
        assert caveat and "never cleared" in caveat

    def test_recovered_is_a_share_of_ever_failed(self):
        """40 of the 140 that ever errored have since completed."""
        g = build(1200, 1000, 900, 100, 140, 40)
        m = g.metrics["recovered"]
        assert m.denominator == 140
        assert m.pct == pytest.approx(28.57, abs=0.01)

    def test_recovered_reconciles_the_two_failure_counts(self):
        """
        The arithmetic a reader should be able to do on the panel:
        ever_failed - recovered == currently_failed, when every error either
        still stands or was recovered.
        """
        g = build(1200, 1000, 900, 100, 140, 40)
        assert (
            g.metrics["ever_failed"].value - g.metrics["recovered"].value
            == g.metrics["currently_failed"].value
        )


class TestUnfinished:
    def test_counts_everything_not_yet_terminal(self):
        g = build(
            total=5000, terminal=1000, completed=900, failed=100,
            ever_failed=140, recovered=40,
        )
        assert g.metrics["unfinished"].value == 4000
        assert g.metrics["unfinished"].denominator == 5000


class TestEnvelopeDiscipline:
    def test_every_metric_carries_a_population_label(self):
        """
        The structural guarantee: nothing in this group can reach a template
        as a bare number.
        """
        g = build(1200, 1000, 900, 100, 140, 40)
        assert g.metrics
        for key, m in g.metrics.items():
            assert m.denominator_label.strip(), f"{key} has no denominator label"
            assert m.as_of.tzinfo is not None, f"{key} has a naive as_of"

    def test_serialises_with_labels_intact(self):
        g = build(1200, 1000, 900, 100, 140, 40)
        d = g.as_dict()
        for key, payload in d["metrics"].items():
            assert payload["denominator_label"], f"{key} lost its label in as_dict"
        assert d["name"] == "reliability"
        assert "as_of" in d

    def test_group_note_explains_the_bucketing_compromise(self):
        """
        The COALESCE(processed_at, updated_at) bucket is a workaround for a
        missing column and must not be silent.
        """
        g = build(1200, 1000, 900, 100, 140, 40)
        assert g.note and "COALESCE(processed_at, updated_at)" in g.note
