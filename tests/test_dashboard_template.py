"""
Static checks over the admin dashboard's inline script.

These exist because of a failure that produced no error anywhere. The template
declared ``loadPipeline`` twice: once with the full set of renderers, and again
200 lines later with a version that drew only the funnel and coverage. Function
declarations hoist, so the later one silently replaced the first, and the key
metrics, cost, status, trend, recent-upload and search panels were simply never
drawn. Every request returned 200, the console stayed clean, and the surviving
function did its own job correctly — there was nothing to notice.

A duplicate top-level function in one script block cannot be intentional, so it
is worth failing the build over rather than discovering it from a screenshot of
panels stuck on "Loading...".
"""

import re
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parent.parent / "templates/admin/dashboard.html"

#: Top-level declarations in the dashboard's inline script are indented by six
#: spaces. Anchoring on that avoids matching nested helpers and callbacks,
#: which are legitimately allowed to share names across different scopes.
TOP_LEVEL_FUNCTION = re.compile(r"^      (?:async )?function ([A-Za-z0-9_$]+)\s*\(", re.M)


@pytest.fixture(scope="module")
def source():
    return TEMPLATE.read_text()


def test_no_duplicate_top_level_functions(source):
    """
    Two declarations of one name mean the first is dead code.

    JavaScript resolves this silently in favour of the last declaration, so the
    only symptom is a panel that never fills in.
    """
    names = TOP_LEVEL_FUNCTION.findall(source)
    duplicates = sorted({n for n in names if names.count(n) > 1})
    assert not duplicates, (
        f"{duplicates} declared more than once at the top level of the dashboard "
        "script. Later declarations silently win, leaving the earlier body "
        "unreachable — merge them."
    )


def test_pipeline_loader_renders_every_zone_the_endpoint_feeds(source):
    """
    /api/metrics/pipeline carries four groups, and each has a renderer.

    The regression dropped six panels while still returning 200, so this pins
    the loader to the payload it receives rather than to a screenshot.
    """
    bodies = re.findall(
        r"async function loadPipeline\(\) \{.*?\n      \}", source, re.S
    )
    assert bodies, "loadPipeline() not found in the dashboard template"
    # Each declaration must be complete, not just the first. When the name was
    # declared twice it was the *last* body that ran, so checking only one
    # match would have passed against the very regression this guards.
    for renderer in (
        "renderKeyMetrics",
        "renderFunnel",
        "renderCoverage",
        "renderCost",
        "renderStatusBreakdown",
        "renderTrends",
        "renderRecentUploads",
        "renderTopSearches",
    ):
        assert all(renderer in b for b in bodies), (
            f"loadPipeline() no longer calls {renderer}; the panel it draws "
            "will stay on its placeholder with no error reported."
        )


def test_pipeline_loader_keeps_drilldown_state(source):
    """
    ``pipelineData`` backs the stage drill-down, which reads the funnel counts
    already fetched instead of issuing its own query. The merged loader has to
    keep setting it or clicking a funnel bar opens an empty panel.
    """
    bodies = re.findall(
        r"async function loadPipeline\(\) \{.*?\n      \}", source, re.S
    )
    assert all("pipelineData =" in b for b in bodies)


def test_pipeline_is_fetched_once_per_load(source):
    """
    The duplicate loader also meant the endpoint appeared twice in the source.
    One expensive corpus-wide payload, fetched once.
    """
    assert source.count('fetch("/api/metrics/pipeline")') == 1
