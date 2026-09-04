"""
Tests for the stage registry and the funnel assembly.

The registry exists to stop one specific class of bug: the old dashboard
implemented "missing embeddings" twice, over different populations, with one
capped at 100 rows, and rendered both on the same screen where they
disagreed. These tests pin the properties that make a second implementation
impossible — the funnel and the drill-down read the same predicate, and the
funnel is monotonic by construction rather than by luck.

Predicate *composition* is tested here by inspecting the compiled criteria.
Whether those predicates select the right rows still needs Postgres — see the
module docstring in test_metrics_foundation.py for why none is available.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.metrics import stages
from services.metrics.pipeline import PipelineMetrics, StageDrilldown


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_funnel_starts_at_an_always_true_root(self):
        root = stages.FUNNEL_STAGES[0]
        assert root.is_root
        assert root.key == "uploaded"

    def test_only_the_root_has_no_predicate(self):
        for st in stages.FUNNEL_STAGES[1:] + stages.COVERAGE_STAGES:
            assert st.predicate is not None, f"{st.key} has no predicate"

    def test_every_stage_explains_what_its_absence_means(self):
        """
        A drill-down opens on a list of documents; the reader needs to know
        what they are looking at without reverse-engineering a SQL predicate.
        """
        for st in stages.ALL_STAGES.values():
            assert st.reading.strip(), f"{st.key} has no reading"
            assert st.missing_label.strip(), f"{st.key} has no missing_label"

    def test_keys_are_unique_across_both_kinds(self):
        keys = [s.key for s in (*stages.FUNNEL_STAGES, *stages.COVERAGE_STAGES)]
        assert len(keys) == len(set(keys))
        assert len(stages.ALL_STAGES) == len(keys)

    def test_lookup_returns_none_for_unknown(self):
        assert stages.get("no_such_stage") is None

    def test_coverage_stages_are_not_funnel_stages(self):
        """
        The distinction is load-bearing: coverage stages are parallel outputs
        of one task, and composing them cumulatively would assert an ordering
        between client, state and date that does not exist.
        """
        for st in stages.COVERAGE_STAGES:
            assert st.kind == "coverage"
        for st in stages.FUNNEL_STAGES:
            assert st.kind == "funnel"


# ---------------------------------------------------------------------------
# Predicate composition
# ---------------------------------------------------------------------------


class TestCumulativeComposition:
    def test_reached_criteria_grow_monotonically(self):
        """
        Each funnel stage ANDs every prior predicate, so a later stage can
        never select a row an earlier one excluded. That is what makes the
        bars monotonic by construction instead of by hope — and it is why an
        out-of-order document shows up as a flagged discrepancy rather than as
        a bar that mysteriously grows.
        """
        lengths = [
            len(stages.reached_criteria(st)) for st in stages.FUNNEL_STAGES
        ]
        assert lengths == sorted(lengths)
        assert lengths[0] == 0  # root filters nothing
        assert lengths[-1] == len(stages.FUNNEL_STAGES) - 1

    def test_last_funnel_stage_includes_every_predicate(self):
        last = stages.FUNNEL_STAGES[-1]
        assert len(stages.reached_criteria(last)) == len(stages.FUNNEL_STAGES) - 1

    def test_coverage_criteria_are_single_and_uncomposed(self):
        for st in stages.COVERAGE_STAGES:
            assert len(stages.reached_criteria(st)) == 1

    def test_independent_criteria_are_always_single(self):
        for st in stages.FUNNEL_STAGES[1:]:
            assert len(stages.independent_criteria(st)) == 1

    def test_root_has_no_criteria_in_any_mode(self):
        root = stages.FUNNEL_STAGES[0]
        assert stages.reached_criteria(root) == []
        assert stages.independent_criteria(root) == []
        assert stages.lost_at_criteria(root) == []


class TestLostAtComposition:
    def test_lost_at_is_priors_plus_negation(self):
        """
        "Lost at stage N" means reached everything before N but not N. The
        count of that set is exactly the funnel's drop, which is why the bar
        and the drill-down list cannot disagree.
        """
        for i, st in enumerate(stages.FUNNEL_STAGES):
            if st.is_root:
                continue
            lost = stages.lost_at_criteria(st)
            # i prior predicates (excluding the root) plus one negation
            assert len(lost) == i, f"{st.key}: expected {i} criteria, got {len(lost)}"

    def test_lost_at_for_first_real_stage_is_just_the_negation(self):
        first = stages.FUNNEL_STAGES[1]
        assert len(stages.lost_at_criteria(first)) == 1

    def test_coverage_lost_at_is_only_the_negation(self):
        for st in stages.COVERAGE_STAGES:
            assert len(stages.lost_at_criteria(st)) == 1

    def test_drop_and_drilldown_use_the_same_priors(self):
        """
        The invariant that ties the bar to the list: lost_at(N) shares its
        prior predicates with reached(N-1), so the drill-down is a subset of
        the stage above and its size is the drop.
        """
        for i in range(2, len(stages.FUNNEL_STAGES)):
            st = stages.FUNNEL_STAGES[i]
            prev = stages.FUNNEL_STAGES[i - 1]
            lost = stages.lost_at_criteria(st)
            reached_prev = stages.reached_criteria(prev)
            assert len(lost) == len(reached_prev) + 1
            for a, b in zip(reached_prev, lost):
                assert str(a) == str(b), f"{st.key}: prior predicates diverged"


# ---------------------------------------------------------------------------
# Funnel assembly
# ---------------------------------------------------------------------------


def build_counts(**overrides):
    """Counts for a plausible corpus, overridable per stage."""
    counts = {"total": 1000}
    running = 1000
    for st in stages.FUNNEL_STAGES:
        if st.is_root:
            continue
        running = max(running - 100, 0)
        counts[f"reached_{st.key}"] = running
        counts[f"has_{st.key}"] = running
    for st in stages.COVERAGE_STAGES:
        counts[f"has_{st.key}"] = 400
    counts.update(overrides)
    return counts


def collect_with(counts):
    svc = PipelineMetrics(MagicMock())
    with patch.object(PipelineMetrics, "_gather_counts", return_value=counts):
        return svc.collect()


class TestFunnelAssembly:
    def test_bars_never_increase(self):
        g = collect_with(build_counts())
        reached = [r["reached"] for r in g.series["funnel"]]
        assert reached == sorted(reached, reverse=True)

    def test_drop_equals_the_difference_from_the_stage_above(self):
        g = collect_with(build_counts())
        rows = g.series["funnel"]
        for prev, row in zip(rows, rows[1:]):
            assert row["lost_here"] == prev["reached"] - row["reached"]

    def test_root_has_no_drop_and_is_not_drillable(self):
        g = collect_with(build_counts())
        root = g.series["funnel"][0]
        assert root["lost_here"] == 0
        assert root["drillable"] is False

    def test_every_non_root_stage_is_drillable(self):
        g = collect_with(build_counts())
        for row in g.series["funnel"][1:]:
            assert row["drillable"] is True

    def test_out_of_order_data_is_surfaced_not_absorbed(self):
        """
        A document holding a later artifact while missing an earlier one — an
        embedding with no extracted text, say — is invisible if only the
        cumulative count is computed. Reporting both makes it a finding.
        """
        counts = build_counts()
        counts["has_embedding"] = counts["reached_embedding"] + 37
        g = collect_with(counts)
        row = next(r for r in g.series["funnel"] if r["key"] == "embedding")
        assert row["out_of_order"] == 37
        assert "missing an earlier stage" in row["out_of_order_note"]

    def test_no_out_of_order_flag_when_counts_agree(self):
        g = collect_with(build_counts())
        assert all("out_of_order" not in r for r in g.series["funnel"])

    def test_empty_corpus_yields_null_percentages_not_zero_bars(self):
        counts = {"total": 0}
        for st in stages.FUNNEL_STAGES:
            if not st.is_root:
                counts[f"reached_{st.key}"] = 0
                counts[f"has_{st.key}"] = 0
        for st in stages.COVERAGE_STAGES:
            counts[f"has_{st.key}"] = 0
        g = collect_with(counts)
        assert all(r["pct_of_corpus"] is None for r in g.series["funnel"])

    def test_fully_processed_is_the_last_funnel_stage(self):
        counts = build_counts()
        g = collect_with(counts)
        last_key = stages.FUNNEL_STAGES[-1].key
        assert g.metrics["fully_processed"].value == counts[f"reached_{last_key}"]
        assert g.metrics["fully_processed"].denominator == counts["total"]


class TestCoverageAssembly:
    def test_present_and_missing_sum_to_the_corpus(self):
        counts = build_counts()
        g = collect_with(counts)
        for cell in g.series["coverage"]:
            assert cell["present"] + cell["missing"] == counts["total"]

    def test_coverage_is_not_ordered_like_a_funnel(self):
        """
        Coverage cells are siblings. Nothing here should assert that one
        implies another, so they are free to be non-monotonic.
        """
        counts = build_counts(has_client=400, has_state=550, has_date=300)
        g = collect_with(counts)
        values = [c["present"] for c in g.series["coverage"]]
        assert values != sorted(values, reverse=True)  # and that is fine

    def test_every_coverage_cell_has_an_envelope_metric(self):
        g = collect_with(build_counts())
        for st in stages.COVERAGE_STAGES:
            m = g.metrics[f"coverage_{st.key}"]
            assert m.denominator_label.strip()
            assert m.denominator == 1000


class TestEnvelopeDiscipline:
    def test_all_metrics_carry_labels_and_aware_timestamps(self):
        g = collect_with(build_counts())
        for key, m in g.metrics.items():
            assert m.denominator_label.strip(), f"{key} unlabelled"
            assert m.as_of.tzinfo is not None, f"{key} naive as_of"

    def test_note_explains_the_two_block_distinction(self):
        g = collect_with(build_counts())
        assert "cumulative" in g.note and "siblings" in g.note


# ---------------------------------------------------------------------------
# Drill-down
# ---------------------------------------------------------------------------


class TestDrilldownGuards:
    def test_unknown_stage_raises_keyerror(self):
        with pytest.raises(KeyError):
            StageDrilldown(MagicMock()).fetch("not_a_stage")

    def test_root_stage_is_rejected_with_an_explanation(self):
        """
        Every document reaches 'uploaded', so there is no missing population
        to list. Returning an empty page would imply there was one.
        """
        with pytest.raises(ValueError, match="funnel root"):
            StageDrilldown(MagicMock()).fetch("uploaded")

    def test_per_page_is_capped(self):
        """A drill-down is for triage, not bulk export."""
        assert StageDrilldown.MAX_PER_PAGE == 200


class TestPredicateCostShape:
    """
    Structural guards on the predicates that dominated a 65-second dashboard
    load. All three columns they touch were still `json` in production, where
    every operator re-parses the whole document per row — so how many times a
    predicate reaches into JSON is a performance property worth pinning.
    """

    def _sql(self, key, needs_cast):
        from sqlalchemy import select
        from sqlalchemy.dialects import postgresql

        from services.metrics import jsonb, stages

        previous = jsonb._needs_cast
        jsonb._needs_cast = needs_cast
        try:
            expr = stages.get(key).predicate()
            return " ".join(
                str(
                    select(expr).compile(
                        dialect=postgresql.dialect(),
                        compile_kwargs={"literal_binds": True},
                    )
                ).split()
            )
        finally:
            jsonb._needs_cast = previous

    def test_no_cast_on_json_columns_in_either_state(self):
        """
        ``json -> jsonb`` parses the document *and* rebuilds it in binary, per
        row. The predicates use native functions chosen by type instead, so
        neither state pays for a cast.
        """
        for key in ("keyword_mappings", "feature_task_ran", "real_summary"):
            for needs_cast in (True, False):
                assert "AS JSONB" not in self._sql(key, needs_cast), (key, needs_cast)

    def test_array_length_matches_the_column_type(self):
        assert "json_array_length" in self._sql("keyword_mappings", True)
        assert "jsonb_array_length" in self._sql("keyword_mappings", False)

    def test_key_presence_needs_no_type_specific_function(self):
        """``-> key IS NOT NULL`` works on both types, so it never branches."""
        for needs_cast in (True, False):
            sql = self._sql("feature_task_ran", needs_cast)
            assert "IS NOT NULL" in sql
            assert "jsonb_exists" not in sql

    def test_summary_extracted_at_most_twice(self):
        """
        Was three times — an explicit IS NOT NULL that SQL's three-valued
        logic already covers. Each extraction is a full parse on a json column.
        """
        assert self._sql("real_summary", True).count("->> 'summary'") <= 2

    def test_summary_still_excludes_null_empty_and_placeholder(self):
        """The simplification must not have widened what counts as a summary."""
        sql = self._sql("real_summary", True)
        assert "!= ''" in sql
        assert "NOT ILIKE" in sql and "no summary available" in sql

    def test_text_presence_avoids_full_detoast(self):
        """
        extracted_text holds full OCR output. Comparing the column to '' 
        detoasts the whole value; asking for one character does not.
        """
        sql = self._sql("extracted_text", True)
        assert "substr(documents.extracted_text, 1, 1)" in sql
