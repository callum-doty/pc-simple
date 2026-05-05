"""
Feature extraction service — single-document orchestration layer.

Three-layer architecture:
  Layer 1 — Extraction:     extract_fields(), _extract_client_from_ai()
  Layer 2 — Transformation: normalize_client(), resolve_canonical(), _merge_clients()
  Layer 3 — Orchestration:  extract_document_features() (called by Celery task)

Canonical map (Option C — DB table as single source of truth):
  load_canonical_map_from_db(db) queries canonical_overrides at task time,
  so corrections take effect immediately for all future documents.
  Pass force=True to the Celery task to reprocess documents after an override change.
"""

import logging
import os
import sys
from datetime import datetime
from difflib import SequenceMatcher
from typing import Optional

# Ensure the project root is on sys.path so feature_extraction is importable
# regardless of how/where the process is launched (uvicorn, celery, etc.)
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from feature_extraction.extract_fields import (
    process_document,
    needs_review_flag,
)
from feature_extraction.extract_from_ai import (
    extract_from_keyword_mappings,
    extract_from_summary,
)
from feature_extraction.normalize_clients import normalize_client
from feature_extraction.apply_canonical_map import resolve_canonical
from feature_extraction.merge_canonical import clean_ai_name, clean_v1_normalize

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical map — loaded from DB at task time (Option C)
# ---------------------------------------------------------------------------

def load_canonical_map_from_db(db) -> dict:
    """
    Returns {client_clean_v1_lower: client_canonical} from canonical_overrides table.
    Called once per Celery task execution so corrections are always current.
    """
    from models.canonical_override import CanonicalOverride
    overrides = db.query(CanonicalOverride).all()
    return {row.client_clean_v1.lower(): row.client_canonical for row in overrides}


# ---------------------------------------------------------------------------
# Layer 1: Extraction
# ---------------------------------------------------------------------------

def _extract_client_from_ai(
    ai_analysis: Optional[dict],
    keyword_mappings: Optional[list],
) -> tuple:
    """
    AI-based client extraction adapted for the DB model, where ai_analysis and
    keyword_mappings are stored in separate JSONB columns (not one JSON string).
    Returns (name, confidence, source_tier).
    """
    mappings = keyword_mappings or []

    summary = ""
    if isinstance(ai_analysis, dict):
        doc_analysis = ai_analysis.get("document_analysis")
        if isinstance(doc_analysis, dict):
            summary = doc_analysis.get("summary", "")
        else:
            summary = ai_analysis.get("summary", "")

    name, confidence = extract_from_keyword_mappings(mappings)
    if name:
        return name, confidence, "tier1_keyword"

    name, confidence = extract_from_summary(summary)
    if name:
        return name, confidence, "tier2_summary"

    return None, None, "no_match"


# ---------------------------------------------------------------------------
# Layer 2: Transformation
# ---------------------------------------------------------------------------

def _similarity(a: Optional[str], b: Optional[str]) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _merge_clients(
    clean_v1: Optional[str],
    ai_name: Optional[str],
    ai_conf: Optional[str],
    ai_tier: Optional[str],
) -> tuple:
    """
    Merge heuristic and AI signals into a single canonical value.
    Returns (client_canonical, canonical_source).

    Agreement boost: if both signals are present and >85% similar, unify on
    the clean_v1 form (more reliable for org identity) and flag the source.
    This reduces false conflicts and unnecessary manual overrides.
    """
    clean = clean_v1_normalize(clean_v1) or clean_v1
    ai = clean_ai_name(ai_name or "")

    if clean and ai and _similarity(clean, ai) > 0.85:
        return clean, "agreement_boost"

    if ai and ai_conf == "HIGH":
        return ai, f"ai_{ai_tier}"

    if ai and ai_conf == "MEDIUM" and not clean:
        return ai, f"ai_{ai_tier}_medium"

    if clean and ai and ai_conf == "MEDIUM":
        return clean, "clean_v1_over_ai_medium"

    if clean:
        return clean, "clean_v1_fallback"

    if ai:
        return ai, f"ai_{ai_tier}_last_resort"

    return None, "null"


# ---------------------------------------------------------------------------
# Layer 3: Orchestration
# ---------------------------------------------------------------------------

def extract_document_features(document, canonical_map: dict) -> dict:
    """
    Run full feature extraction for a single document.

    canonical_map must be loaded by the caller via load_canonical_map_from_db(db)
    so that corrections in canonical_overrides are always current.

    Returns a dict of column values ready to write to the Document model,
    plus a '_meta' key with traceability data for file_metadata storage.
    """
    text = document.extracted_text or ""
    ai_analysis = document.ai_analysis or {}
    keyword_mappings = []
    if isinstance(document.keywords, dict):
        keyword_mappings = document.keywords.get("keyword_mappings", [])

    # --- Layer 1: Extract ---
    fields = process_document(text)
    ai_name, ai_conf, ai_tier = _extract_client_from_ai(ai_analysis, keyword_mappings)

    # --- Layer 2: Transform ---
    clean_v1 = normalize_client(fields.get("paid_for_by_raw"))
    resolved, rules_source = resolve_canonical(clean_v1, canonical_map)
    final_canonical, canonical_source = _merge_clients(resolved, ai_name, ai_conf, ai_tier)

    review_fields = {
        "date_confidence": fields.get("date_confidence"),
        "candidate_confidence": fields.get("candidate_confidence"),
        "state_confidence": fields.get("state_confidence"),
    }

    date_created = None
    raw_date = fields.get("predicted_date")
    if raw_date:
        try:
            date_created = datetime.strptime(raw_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    return {
        "paid_for_by_raw": fields.get("paid_for_by_raw"),
        "is_frank": fields.get("is_frank", False),
        "client": fields.get("predicted_candidate"),
        "client_clean_v1": clean_v1,
        "client_canonical": final_canonical,
        "client_confidence": fields.get("candidate_confidence"),
        "state": fields.get("predicted_state"),
        "state_confidence": fields.get("state_confidence"),
        "date_created": date_created,
        "date_confidence": fields.get("date_confidence"),
        "needs_review": needs_review_flag(review_fields),
        "_meta": {
            "canonical_source": canonical_source,
            "rules_source": rules_source,
            "ai_confidence": ai_conf,
            "ai_tier": ai_tier,
            "extraction_version": 1,
        },
    }
