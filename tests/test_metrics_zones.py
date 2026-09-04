"""
Tests for Zones 3-5 and activity — the assembly logic and the honesty rules.

As elsewhere in this suite, population counting is stubbed: the Document model
uses Postgres-only column types and no Postgres is available here. What is
exercised is the reasoning built on top of the counts — the three-way franking
split, the NULL confidence bucket, the status breakdown summing to the corpus,
and the caveats that must travel with particular numbers.

Predicate correctness (does this SQL select the right rows?) is verified
separately by compiling every query against the Postgres dialect, and finally
by running scripts/profile_data.py against a real database.
"""

from unittest.mock import MagicMock, patch

import pytest

from services.metrics import scope
from services.metrics.activity import FILTER_ONLY_SENTINEL, ActivityMetrics
from services.metrics.corpus import CorpusMetrics
from services.metrics.cost import CostMetrics
from services.metrics.quality import CONFIDENCE_CAVEAT, QualityMetrics


# ---------------------------------------------------------------------------
# Zone 3 — cost
# ---------------------------------------------------------------------------


def cost_group(docs_with_cost=620, total=1000, **series):
    svc = CostMetrics(MagicMock())
    with patch.object(
        CostMetrics, "_totals",
        return_value={"docs": docs_with_cost, "input_tokens": 48_231_904,
                      "output_tokens": 3_104_552},
    ), patch.object(
        CostMetrics, "_percentiles", return_value={"p50": 62_000, "p95": 410_000}
    ), patch.object(
        CostMetrics, "_by_provider", return_value=series.get("by_provider", [])
    ), patch.object(
        CostMetrics, "_by_client", return_value=series.get("by_client", [])
    ), patch.object(
        CostMetrics, "_by_month", return_value=series.get("by_month", [])
    ), patch(
        "services.metrics.cost.scope.corpus_total", return_value=total
    ):
        return svc.collect()


class TestCost:
    def test_coverage_is_a_share_of_the_whole_corpus(self):
        g = cost_group(docs_with_cost=620, total=1000)
        m = g.metrics["coverage"]
        assert m.value == 620
        assert m.denominator == 1000
        assert m.pct == 62.0

    def test_every_total_carries_the_partial_path_caveat(self):
        """
        Only the chunked PDF path writes processing_cost, so a corpus-wide
        total undercounts. That must travel with the number, not sit in a
        footnote nobody reads.
        """
        g = cost_group()
        for key in ("coverage", "input_tokens", "output_tokens"):
            assert "chunked PDF path" in (g.metrics[key].caveat or ""), key

    def test_token_totals_are_scoped_to_documents_with_a_record(self):
        g = cost_group(docs_with_cost=620)
        assert g.metrics["input_tokens"].denominator == 620
        assert g.metrics["input_tokens"].scope == "with_cost"

    def test_percentiles_not_a_mean(self):
        g = cost_group()
        assert g.metrics["tokens_p50"].value == 62_000
        assert g.metrics["tokens_p95"].value == 410_000
        assert "percentiles, not a mean" in (g.metrics["tokens_p95"].caveat or "")

    def test_note_explains_tokens_rather_than_currency(self):
        g = cost_group()
        assert "not currency" in g.note

    def test_zero_coverage_does_not_divide_by_zero(self):
        g = cost_group(docs_with_cost=0, total=0)
        assert g.metrics["coverage"].pct is None
        assert g.metrics["input_tokens"].pct is None


# ---------------------------------------------------------------------------
# Zone 5 — quality
# ---------------------------------------------------------------------------


def quality_group(total=1000, review=None, embeddings=None, curation=None,
                  duplicates=None, confidence=None):
    svc = QualityMetrics(MagicMock())
    review = review or {"either": 612, "needs_review": 508,
                        "date_range_violation": 147, "both": 43}
    embeddings = embeddings or {"never_embedded": 250, "unversioned": 10,
                                "behind_current": 252, "current_version": 3}
    curation = curation or {"total": 84, "last_30_days": 12}
    duplicates = duplicates or {"duplicate_groups": 7, "hashed_documents": 800}
    confidence = confidence if confidence is not None else [
        {"field": "date_confidence", "label": "Date", "high": 211, "medium": 98,
         "low": 50, "not_extracted": 641, "unrecognised": 0, "total": total}
    ]
    with patch.object(QualityMetrics, "_review_queue", return_value=review), \
         patch.object(QualityMetrics, "_embedding_staleness", return_value=embeddings), \
         patch.object(QualityMetrics, "_curation_effort", return_value=curation), \
         patch.object(QualityMetrics, "_duplicates", return_value=duplicates), \
         patch.object(QualityMetrics, "_confidence_distributions", return_value=confidence), \
         patch("services.metrics.quality.scope.corpus_total", return_value=total):
        return svc.collect()


class TestQuality:
    def test_review_backlog_matches_the_nav_badge_definition(self):
        """
        The badge counts needs_review OR needs_date_review; the old dashboard
        card counted only the first, so the two disagreed about the size of
        the same queue.
        """
        g = quality_group()
        assert g.metrics["review_backlog"].value == 612
        rows = {r["key"]: r for r in g.series["review_queue"]}
        assert rows["either"]["count"] == 612
        assert "needs_review OR needs_date_review" in rows["either"]["note"]

    def test_date_flag_is_labelled_as_a_range_violation(self):
        """
        needs_date_review fires only when a date parsed and fell outside
        2019-2026. A document with no date is not flagged, and the label must
        not imply otherwise.
        """
        g = quality_group()
        m = g.metrics["date_range_violations"]
        assert "outside 2019-2026" in m.denominator_label
        assert "no date at all is not flagged" in (m.caveat or "")

    def test_confidence_carries_the_human_verification_caveat(self):
        g = quality_group()
        assert g.note == CONFIDENCE_CAVEAT
        assert "human verification" in g.note

    def test_null_confidence_is_a_visible_segment(self):
        """
        The old bars filtered NULL out, so a corpus that was mostly
        unextracted could show 90% HIGH. Here the NULL bucket is a segment.
        """
        g = quality_group()
        row = g.series["confidence"][0]
        assert row["not_extracted"] == 641
        assert row["high"] + row["medium"] + row["low"] + row["not_extracted"] == row["total"]

    def test_duplicates_scoped_to_hashed_documents(self):
        g = quality_group()
        m = g.metrics["duplicate_groups"]
        assert m.denominator == 800
        assert "Dropbox-ingested" in (m.caveat or "")

    def test_embedding_staleness_omitted_when_version_unknown(self):
        """
        If the current embedding version cannot be read, reporting "0 behind"
        would be a confident false statement. The metric is absent instead.
        """
        g = quality_group(embeddings={"never_embedded": 5, "unversioned": 0,
                                      "behind_current": None, "current_version": None})
        assert "embeddings_behind" not in g.metrics
        assert "never_embedded" in g.metrics

    def test_missing_overrides_table_omits_the_metric(self):
        g = quality_group(curation={"total": None, "last_30_days": None})
        assert "canonical_overrides" not in g.metrics


# ---------------------------------------------------------------------------
# Zone 4 — corpus
# ---------------------------------------------------------------------------


class TestFranking:
    """
    is_frank is nullable with default=False and only the feature task writes
    it, so FALSE means "not franked" or "never examined". The old panel
    divided TRUE by every row and reported one number, understating franking
    by exactly the unextracted share.
    """

    def _frank(self, total=1000, extracted=400, franked=124, not_franked=276, nulls=600):
        svc = CorpusMetrics(MagicMock())
        db = MagicMock()
        row = MagicMock()
        row.franked, row.not_franked, row.null_frank = franked, not_franked, nulls
        db.query.return_value.one.return_value = row
        svc.db = db
        # The extracted count is now passed in rather than re-queried: collect()
        # already has it, and issuing the identical scan twice was pure waste.
        return svc._franking(total, extracted)

    def test_reports_three_buckets(self):
        f = self._frank()
        assert f["franked"] == 124
        assert f["not_franked"] == 276
        assert f["null"] == 600

    def test_reports_two_rates_not_one(self):
        """
        Over the corpus the rate is a floor; over extracted documents it is
        the best estimate. Which is right depends on the question, so both
        are given and both are labelled.
        """
        f = self._frank(total=1000, extracted=400, franked=124)
        assert f["pct_of_corpus"] == 12.4
        assert f["pct_of_extracted"] == 31.0

    def test_corpus_rate_is_never_above_the_extracted_rate(self):
        f = self._frank()
        assert f["pct_of_corpus"] <= f["pct_of_extracted"]

    def test_no_division_by_zero_on_an_empty_corpus(self):
        f = self._frank(total=0, extracted=0, franked=0, not_franked=0, nulls=0)
        assert f["pct_of_corpus"] is None
        assert f["pct_of_extracted"] is None


class TestGeography:
    def _geo(self, rows, total):
        svc = CorpusMetrics(MagicMock())
        db = MagicMock()
        db.query.return_value.filter.return_value.group_by.return_value.order_by.return_value.all.return_value = rows
        svc.db = db
        return svc._geography(total)

    def test_appends_an_explicit_no_state_row(self):
        """
        The old chart divided by documents that had a state, so states summed
        to 100% however little of the corpus was placed. The missing mass is
        now a row.
        """
        rows = [MagicMock(state="MI", docs=300), MagicMock(state="PA", docs=100)]
        out = self._geo(rows, total=1000)
        assert out[-1]["state"] == "no state"
        assert out[-1]["docs"] == 600
        assert out[-1]["missing"] is True

    def test_percentages_are_of_the_whole_corpus(self):
        rows = [MagicMock(state="MI", docs=300)]
        out = self._geo(rows, total=1000)
        assert out[0]["pct_of_corpus"] == 30.0

    def test_all_rows_sum_to_the_corpus(self):
        rows = [MagicMock(state="MI", docs=300), MagicMock(state="PA", docs=100)]
        out = self._geo(rows, total=1000)
        assert sum(r["docs"] for r in out) == 1000


# ---------------------------------------------------------------------------
# Activity
# ---------------------------------------------------------------------------


def activity_group(total=1000, status_rows=None, search=None):
    svc = ActivityMetrics(MagicMock())
    search = search or {"total": 480, "filter_only": 60, "zero_result": 44,
                        "used_client": 120, "used_state": 90, "used_year": 30}
    status_rows = status_rows if status_rows is not None else [
        {"status": s, "docs": n}
        for s, n in zip(scope.ALL_STATUSES, [40, 5, 2, 900, 53])
    ]
    with patch.object(ActivityMetrics, "_status_breakdown", return_value=status_rows), \
         patch.object(ActivityMetrics, "_trends", return_value=[]), \
         patch.object(ActivityMetrics, "_recent_uploads", return_value=[]), \
         patch.object(ActivityMetrics, "_top_terms", return_value=[]), \
         patch.object(ActivityMetrics, "_search_summary", return_value=search), \
         patch("services.metrics.activity.scope.corpus_total", return_value=total):
        svc.db = MagicMock()
        svc.db.query.return_value.filter.return_value.scalar.return_value = 120
        return svc.collect()


class TestActivity:
    def test_search_metrics_carry_the_logging_caveat(self):
        """
        Logging sits after the cache early-return and each pagination click
        logs a row, so the figure is wrong in both directions and no column
        lets us correct it.
        """
        g = activity_group()
        for key in ("searches_30d", "zero_result_searches", "filter_only_searches"):
            caveat = g.metrics[key].caveat or ""
            assert "cache hits" in caveat and "pagination" in caveat, key

    def test_zero_result_is_a_share_of_logged_searches(self):
        g = activity_group()
        m = g.metrics["zero_result_searches"]
        assert m.denominator == 480
        assert "logged searches" in m.denominator_label

    def test_sentinel_is_named_as_the_service_writes_it(self):
        assert FILTER_ONLY_SENTINEL == "(filter only)"


class TestStatusBreakdown:
    """
    The old doughnut charted PENDING, PROCESSING, COMPLETED and FAILED,
    omitting QUEUED — the one status documents actually arrive in — so its
    slices did not sum to the corpus.
    """

    def _breakdown(self, counts, total):
        svc = ActivityMetrics(MagicMock())
        row = MagicMock()
        row._mapping = dict(zip(scope.ALL_STATUSES, counts))
        db = MagicMock()
        db.query.return_value.one.return_value = row
        svc.db = db
        return svc._status_breakdown(total)

    def test_includes_queued(self):
        out = self._breakdown([40, 5, 2, 900, 53], 1000)
        assert "QUEUED" in [r["status"] for r in out]

    def test_covers_every_known_status(self):
        out = self._breakdown([40, 5, 2, 900, 53], 1000)
        assert [r["status"] for r in out] == list(scope.ALL_STATUSES)

    def test_slices_sum_to_the_corpus(self):
        out = self._breakdown([40, 5, 2, 900, 53], 1000)
        assert sum(r["docs"] for r in out) == 1000

    def test_unknown_status_becomes_a_visible_slice(self):
        """
        A status outside the known set would otherwise make the chart quietly
        fail to add up. It gets its own slice instead.
        """
        out = self._breakdown([40, 5, 2, 900, 3], 1000)
        other = [r for r in out if r["status"] == "OTHER"]
        assert other and other[0]["docs"] == 50
        assert sum(r["docs"] for r in out) == 1000


class TestNaiveCutoff:
    def test_ago_naive_is_naive(self):
        """
        search_queries.timestamp is the one naive column in the schema.
        Comparing it to an aware cutoff makes Postgres coerce using the
        session TimeZone, so the same query would slice differently depending
        on server configuration.
        """
        assert scope.ago_naive(days=30).tzinfo is None

    def test_ago_is_still_aware(self):
        assert scope.ago(days=30).tzinfo is not None

    def test_the_two_describe_the_same_instant(self):
        aware = scope.ago(days=30)
        naive = scope.ago_naive(days=30)
        assert abs((aware.replace(tzinfo=None) - naive).total_seconds()) < 2


# ---------------------------------------------------------------------------
# Cached payloads
# ---------------------------------------------------------------------------


class TestPayloadCache:
    """
    Building these takes about a minute on production — measured, not guessed.
    They are refreshed by a beat task and served from Redis, so the cost stays
    off the request path.
    """

    def test_cache_hit_is_marked_and_not_rebuilt(self):
        from unittest.mock import MagicMock, patch

        from services.metrics import payloads

        stored = {"success": True, "pipeline": {}, "generated_at": "2026-09-04T00:00:00+00:00"}
        with patch.object(payloads, "read", return_value={**stored, "cached": True}), \
             patch.object(payloads, "build_pipeline") as builder:
            out = payloads.get_or_build("pipeline", MagicMock())
        assert out["cached"] is True
        builder.assert_not_called()

    def test_cache_miss_builds_and_stores(self):
        """
        A stopped beat, or an unreachable Redis, must leave the dashboard slow
        rather than broken.
        """
        from unittest.mock import MagicMock, patch

        from services.metrics import payloads

        # Patch through BUILDERS, not the module attribute: the registry holds
        # the function object captured at import, and that is the reference
        # get_or_build actually dispatches through.
        builder = MagicMock(return_value={"success": True, "corpus": {}})
        with patch.object(payloads, "read", return_value=None), \
             patch.dict(payloads.BUILDERS, {"corpus": builder}), \
             patch.object(payloads, "write") as writer:
            out = payloads.get_or_build("corpus", MagicMock())
        builder.assert_called_once()
        writer.assert_called_once()
        assert out["cached"] is False

    def test_refresh_continues_after_one_builder_fails(self):
        """
        One broken zone must not stop the other refreshing — otherwise a single
        failure re-exposes every panel to a cold rebuild.
        """
        from unittest.mock import MagicMock, patch

        from services.metrics import payloads

        with patch.dict(
            payloads.BUILDERS,
            {
                "pipeline": MagicMock(side_effect=RuntimeError("boom")),
                "corpus": MagicMock(return_value={"success": True}),
            },
            clear=True,
        ), patch.object(payloads, "write") as writer:
            results = payloads.refresh_all(MagicMock())

        assert results["pipeline"].startswith("failed")
        assert results["corpus"] == "ok"
        assert writer.call_count == 1

    def test_refresh_interval_is_inside_the_ttl(self):
        """
        A single missed refresh must not expose a cold rebuild, so the beat
        interval has to be comfortably shorter than how long a payload lives.
        """
        from services.metrics import payloads

        assert payloads.REFRESH_SECONDS * 2 < payloads.CACHE_SECONDS


# ---------------------------------------------------------------------------
# Topic aggregation keys
# ---------------------------------------------------------------------------


class TestTopicMappingKeys:
    """
    The topic query must read the keys the pipeline actually writes.

    Inside a ``keyword_mappings`` entry the fields are ``mapped_primary_category``
    and ``mapped_subcategory`` — the shape ``prompt_manager`` asks the model for
    and ``ai_service`` reads back. The bare ``primary_category``/``subcategory``
    are taxonomy.csv's headers and the TaxonomyTerm columns, which is why they
    were easy to reach for by mistake.

    Reaching for them cost the panel silently: ``->>`` on an absent key is SQL
    NULL rather than an error, the IS NOT NULL guards discarded every row, and
    the query returned zero rows successfully. The panel showed "No data."
    against a corpus full of mappings, with a 200 and an empty log — there was
    no failure anywhere to notice.
    """

    def _topic_sql(self):
        import inspect

        from services.metrics.corpus import CorpusMetrics

        return inspect.getsource(CorpusMetrics._topic_levels)

    def test_reads_the_mapped_prefixed_keys(self):
        sql = self._topic_sql()
        assert "mapping ->> 'mapped_primary_category'" in sql
        assert "mapping ->> 'mapped_subcategory'" in sql

    def test_does_not_read_the_taxonomy_table_column_names(self):
        """
        The bare names are a different thing — taxonomy.csv's header. Reading
        them from a mapping yields NULL for every row, not an error.
        """
        sql = self._topic_sql()
        assert "mapping ->> 'primary_category'" not in sql
        assert "mapping ->> 'subcategory'" not in sql

    def test_keys_match_what_the_extraction_pipeline_writes(self):
        """
        Pins the metrics reader to the writer. A rename on either side that
        does not touch the other fails here rather than emptying the panel.
        """
        import inspect

        from services import ai_service

        writer = inspect.getsource(ai_service)
        for key in ("mapped_primary_category", "mapped_subcategory"):
            assert key in writer, (
                f"{key} is no longer written by ai_service; the topic "
                "aggregation reads it and will silently return nothing."
            )
