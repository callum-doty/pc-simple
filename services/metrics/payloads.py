"""
Dashboard payload construction and its Redis cache.

Extracted from api/metrics.py so the Celery beat worker can build and store
the same payloads the API serves, without importing the FastAPI layer.

WHY THE WORKER BUILDS THEM

Measured on production: the pipeline collector takes ~46s and corpus ~18s,
almost entirely re-parsing three ``json`` columns across 9,366 documents.
Even with a five-minute cache that means one unlucky request per five minutes
waits three quarters of a minute, which is not a usable dashboard.

These are corpus-wide aggregates over data that changes on the timescale of
documents being processed. Recomputing them on a schedule and serving the
stored result is the ordinary answer for that shape of metric — the cost moves
off the request path onto a worker that has nothing else to do.

The API still computes on a cache miss, so a stopped beat degrades the
dashboard to slow rather than broken. Every payload carries its own
``generated_at`` and the response reports ``cached``, so a reader can always
see how old the figures are.
"""

import json
import logging
from typing import Callable, Dict, Optional

import redis as redis_lib

from config import get_settings
from services.metrics import jsonb, scope

logger = logging.getLogger(__name__)
settings = get_settings()

#: Namespace for cached metric payloads. Included in the /api/admin/clear-cache
#: sweep alongside search:* and facets:*.
CACHE_PREFIX = "metrics:"

#: How long a stored payload stays servable. Comfortably longer than the
#: refresh interval below, so a single missed refresh does not expose users to
#: a cold rebuild.
CACHE_SECONDS = 900

#: How often the beat worker refreshes. Well inside CACHE_SECONDS.
REFRESH_SECONDS = 240


def cache_client() -> Optional[redis_lib.Redis]:
    if not settings.redis_url:
        return None
    try:
        client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        return client
    except Exception as e:
        logger.warning(f"Metrics cache unavailable: {e}")
        return None


# ---------------------------------------------------------------------------
# Builders
#
# Each detects the JSON column types first, exactly as the API handlers do, so
# a payload built by the worker uses the same query shapes as one built by a
# request.
# ---------------------------------------------------------------------------


def build_pipeline(db) -> dict:
    from services.metrics.activity import ActivityMetrics
    from services.metrics.cost import CostMetrics
    from services.metrics.pipeline import PipelineMetrics
    from services.metrics.reliability import ReliabilityMetrics

    jsonb.configure(db)
    return {
        "success": True,
        "pipeline": PipelineMetrics(db).collect().as_dict(),
        "reliability": ReliabilityMetrics(db).collect().as_dict(),
        "cost": CostMetrics(db).collect().as_dict(),
        "activity": ActivityMetrics(db).collect().as_dict(),
        "generated_at": scope.now_utc().isoformat(),
    }


def build_corpus(db) -> dict:
    from services.metrics.corpus import CorpusMetrics
    from services.metrics.quality import QualityMetrics

    jsonb.configure(db)
    return {
        "success": True,
        "corpus": CorpusMetrics(db).collect().as_dict(),
        "quality": QualityMetrics(db).collect().as_dict(),
        "generated_at": scope.now_utc().isoformat(),
    }


#: The payloads a warm dashboard needs, by cache key.
BUILDERS: Dict[str, Callable] = {
    "pipeline": build_pipeline,
    "corpus": build_corpus,
}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def read(key: str) -> Optional[dict]:
    client = cache_client()
    if client is None:
        return None
    try:
        hit = client.get(f"{CACHE_PREFIX}{key}")
    except Exception as e:
        logger.warning(f"Metrics cache read failed for {key}: {e}")
        return None
    if not hit:
        return None
    try:
        payload = json.loads(hit)
    except Exception as e:
        logger.warning(f"Metrics cache holds unreadable JSON for {key}: {e}")
        return None
    payload["cached"] = True
    return payload


def write(key: str, payload: dict) -> None:
    client = cache_client()
    if client is None:
        return
    try:
        client.set(
            f"{CACHE_PREFIX}{key}",
            json.dumps(payload, default=str),
            ex=CACHE_SECONDS,
        )
    except Exception as e:
        logger.warning(f"Metrics cache write failed for {key}: {e}")


def get_or_build(key: str, db) -> dict:
    """
    Serve a stored payload, or build one and store it.

    A miss, an unreachable Redis, or a stopped beat all fall through to
    building on the request — metrics degrade to slower, never to wrong.
    """
    cached = read(key)
    if cached is not None:
        return cached

    logger.info(
        "Metrics: cache miss for %s, building on the request path. If this is "
        "frequent, check that celery-beat is running.",
        key,
    )
    payload = BUILDERS[key](db)
    payload["cached"] = False
    write(key, payload)
    return payload


def refresh_all(db) -> dict:
    """
    Rebuild and store every payload. Called by the beat task.

    One builder failing must not prevent the others from refreshing, so each
    is attempted independently and the outcome reported per key.
    """
    results = {}
    for key, builder in BUILDERS.items():
        try:
            payload = builder(db)
            payload["cached"] = False
            write(key, payload)
            results[key] = "ok"
        except Exception as e:
            logger.error(f"Metrics refresh failed for {key}: {e}", exc_info=True)
            results[key] = f"failed: {e}"
            try:
                db.rollback()
            except Exception:
                pass
    return results
