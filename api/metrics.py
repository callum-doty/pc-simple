"""
Dashboard metrics API.

Routes:
  GET /api/metrics/now              Zone 0 — live state, safe to poll
  GET /api/metrics/pipeline         Zones 1-3 — funnel, reliability, cost
  GET /api/metrics/corpus           Zones 4-5 — archive contents and quality
  GET /api/metrics/stage/{key}      Drill-down behind one funnel or coverage bar

Replaces api/dashboard.py and services/dashboard_service.py, both removed at
the end of the rebuild. Their thirteen metric methods each chose their own
population, which is how two cards labelled "total" disagreed and how the same
concept read 100 in one panel and thousands in another.

Every response body is composed of ``Metric`` envelopes, so each number
carries the population it was computed over. Handlers here do no arithmetic —
if a number needs deriving, it belongs in services/metrics/.

The handlers are deliberately ``def``, not ``async def``. Every collector does
blocking SQLAlchemy I/O and awaits nothing, so declaring them ``async`` ran
that blocking work directly on the event loop — one slow query froze the whole
process, including ``/health``, which touches no database at all. Render's
health check then timed out and restarted the service, which is exactly what
happened while the json-to-jsonb conversion held its table lock.

FastAPI runs a plain ``def`` path operation in a threadpool, so a slow or
blocked query costs one worker thread instead of the entire application.
"""

import json
import logging
from typing import Optional

import redis as redis_lib
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from config import get_settings
from database import get_db
from services.metrics import jsonb, scope
from services.metrics.activity import ActivityMetrics
from services.metrics.corpus import CorpusMetrics
from services.metrics.cost import CostMetrics
from services.metrics.now import NowMetrics
from services.metrics.pipeline import PipelineMetrics, StageDrilldown
from services.metrics.quality import QualityMetrics
from services.metrics.reliability import ReliabilityMetrics

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()

#: Cache TTL for the heavy zones. Zone 0 is deliberately uncached — it is the
#: only zone whose whole purpose is to be current.
PIPELINE_CACHE_SECONDS = 60

#: Namespace for cached metric payloads. Add this to the /api/admin/clear-cache
#: sweep alongside search:* and facets:*.
CACHE_PREFIX = "metrics:"


def _cache_client() -> Optional[redis_lib.Redis]:
    if not settings.redis_url:
        return None
    try:
        client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception as e:
        logger.warning(f"Metrics cache unavailable, serving uncached: {e}")
        return None


def _cached(key: str, builder, ttl: int = PIPELINE_CACHE_SECONDS) -> dict:
    """
    Serve a metric payload from Redis, or build and store it.

    A cache miss or an unreachable Redis both fall through to building the
    payload — metrics degrade to slower, never to wrong. The stored payload
    keeps its original ``as_of``, so a cached response reports its true age
    rather than implying it was read now.
    """
    client = _cache_client()
    full_key = f"{CACHE_PREFIX}{key}"

    if client is not None:
        try:
            hit = client.get(full_key)
            if hit:
                payload = json.loads(hit)
                payload["cached"] = True
                return payload
        except Exception as e:
            logger.warning(f"Metrics cache read failed for {full_key}: {e}")

    payload = builder()
    payload["cached"] = False

    if client is not None:
        try:
            client.set(full_key, json.dumps(payload, default=str), ex=ttl)
        except Exception as e:
            logger.warning(f"Metrics cache write failed for {full_key}: {e}")

    return payload


@router.get(
    "/metrics/now",
    summary="Zone 0 — live operational state",
    tags=["Metrics"],
)
def get_now_metrics(db: Session = Depends(get_db)):
    """
    Live pipeline state: leases against status, broker against backlog,
    zombies, ingest freshness, and duration percentiles.

    Uncached and cheap — Redis reads plus indexed counts. Intended to be
    polled every 15s. Each pair carries a verdict interpreting the divergence
    between its two numbers, because for these gauges the disagreement is more
    informative than either figure alone.
    """
    try:
        # One catalog query per process, cached: decides whether the JSON
        # predicates below need a ::jsonb cast. See services/metrics/jsonb.
        jsonb.configure(db)
        return {"success": True, **NowMetrics(db).collect().as_dict()}
    except Exception as e:
        logger.error(f"Error collecting now metrics: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Internal server error collecting live metrics."
        )


@router.get(
    "/metrics/pipeline",
    summary="Zones 1-3 — funnel, reliability, cost",
    tags=["Metrics"],
)
def get_pipeline_metrics(db: Session = Depends(get_db)):
    """
    Processing outcomes, and (once stages 3-4 land) the pipeline funnel and
    token cost.

    Cached for 60s under ``metrics:pipeline``. The response reports whether it
    was served from cache and carries the ``as_of`` of the underlying read.
    """
    try:
        # One catalog query per process, cached: decides whether the JSON
        # predicates below need a ::jsonb cast. See services/metrics/jsonb.
        jsonb.configure(db)
        def build() -> dict:
            return {
                "success": True,
                "pipeline": PipelineMetrics(db).collect().as_dict(),
                "reliability": ReliabilityMetrics(db).collect().as_dict(),
                "cost": CostMetrics(db).collect().as_dict(),
                "activity": ActivityMetrics(db).collect().as_dict(),
                "generated_at": scope.now_utc().isoformat(),
            }

        return _cached("pipeline", build)
    except Exception as e:
        logger.error(f"Error collecting pipeline metrics: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Internal server error collecting pipeline metrics."
        )


@router.get(
    "/metrics/stage/{stage_key}",
    summary="Documents lost at one pipeline stage",
    tags=["Metrics"],
)
def get_stage_drilldown(
    stage_key: str,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=StageDrilldown.MAX_PER_PAGE),
    db: Session = Depends(get_db),
):
    """
    The documents behind a funnel drop or a coverage gap.

    Returns a real COUNT(*) alongside the requested page, and reads the same
    stage predicate the funnel counts with — so the list length and the bar's
    drop are the same number by construction. The endpoint this replaces,
    /api/incomplete-documents, ran LIMIT 100 and reported the length of the
    result as the count.

    Uncached: it is opened deliberately, from a specific bar, and a stale
    triage list is worse than a slightly slower one.
    """
    try:
        # One catalog query per process, cached: decides whether the JSON
        # predicates below need a ::jsonb cast. See services/metrics/jsonb.
        jsonb.configure(db)
        return {"success": True, **StageDrilldown(db).fetch(stage_key, page, per_page)}
    except KeyError:
        from services.metrics import stages

        raise HTTPException(
            status_code=404,
            detail=(
                f"Unknown stage '{stage_key}'. Known stages: "
                f"{', '.join(sorted(stages.ALL_STAGES))}"
            ),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error in stage drilldown '{stage_key}': {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Internal server error building stage drilldown."
        )


@router.get(
    "/metrics/corpus",
    summary="Zones 4-5 — what is in the archive, and how good it is",
    tags=["Metrics"],
)
def get_corpus_metrics(db: Session = Depends(get_db)):
    """
    Clients, geography, timeline, topics, franking and the free dimensions,
    plus confidence, review backlog, embedding staleness and curation effort.

    The heaviest endpoint — the topic aggregation unnests a JSONB array across
    the corpus — so it is cached for 60s under ``metrics:corpus``.
    """
    try:
        # One catalog query per process, cached: decides whether the JSON
        # predicates below need a ::jsonb cast. See services/metrics/jsonb.
        jsonb.configure(db)
        def build() -> dict:
            return {
                "success": True,
                "corpus": CorpusMetrics(db).collect().as_dict(),
                "quality": QualityMetrics(db).collect().as_dict(),
                "generated_at": scope.now_utc().isoformat(),
            }

        return _cached("corpus", build)
    except Exception as e:
        logger.error(f"Error collecting corpus metrics: {e}", exc_info=True)
        raise HTTPException(
            status_code=500, detail="Internal server error collecting corpus metrics."
        )
