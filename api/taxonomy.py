"""
API for taxonomy-related operations.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Dict, List

from database import get_db
from services.taxonomy_service import TaxonomyService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/taxonomy/canonical-terms",
    response_model=Dict[str, List[str]],
    summary="Get all canonical terms grouped by category",
    tags=["Taxonomy"],
)
async def get_all_canonical_terms(db: Session = Depends(get_db)):
    """
    Retrieve all canonical terms, grouped by their primary category.
    This is useful for populating filter dropdowns or other UI elements.
    """
    try:
        service = TaxonomyService(db)
        terms = await service.get_all_canonical_terms()
        return {"success": True, "terms": terms}
    except Exception as e:
        logger.error(f"Error getting all canonical terms: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get(
    "/taxonomy/search-terms",
    response_model=Dict[str, List[str]],
    summary="Search canonical terms",
    tags=["Taxonomy"],
)
async def search_canonical_terms(
    q: str = Query(None, description="Search query for canonical terms"),
    db: Session = Depends(get_db),
):
    """
    Search for canonical terms by a query string.
    Returns a dictionary of terms grouped by their primary category.
    """
    try:
        service = TaxonomyService(db)
        terms = await service.search_canonical_terms(q)
        return {"success": True, "terms": terms}
    except Exception as e:
        logger.error(f"Error searching canonical terms: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error")
