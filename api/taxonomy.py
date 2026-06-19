"""
Taxonomy API routes — moved from main.py as part of FIX-006.

Handles:
  GET /api/taxonomy/categories
  GET /api/taxonomy/categories/{primary_category}/subcategories
  GET /api/taxonomy/hierarchy
  GET /api/taxonomy/filter-data
  GET /api/taxonomy/canonical-terms
  GET /api/taxonomy/search
  GET /api/taxonomy/stats
"""

import logging

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import get_taxonomy_service
from services.taxonomy_service import TaxonomyService

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/taxonomy/categories")
async def get_taxonomy_categories(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get all primary categories from taxonomy."""
    try:
        categories = await taxonomy_service.get_primary_categories()
        return {"success": True, "categories": categories}
    except Exception as e:
        logger.error(f"Taxonomy categories error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/taxonomy/categories/{primary_category}/subcategories")
async def get_taxonomy_subcategories(
    primary_category: str,
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get subcategories for a primary category."""
    try:
        subcategories = await taxonomy_service.get_subcategories(primary_category)
        return {"success": True, "subcategories": subcategories}
    except Exception as e:
        logger.error(f"Taxonomy subcategories error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/taxonomy/hierarchy")
async def get_taxonomy_hierarchy(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get complete taxonomy hierarchy."""
    try:
        hierarchy = await taxonomy_service.get_taxonomy_hierarchy()
        return {"success": True, "hierarchy": hierarchy}
    except Exception as e:
        logger.error(f"Taxonomy hierarchy error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/taxonomy/filter-data")
async def get_filter_taxonomy(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get taxonomy data structured for UI filters."""
    try:
        filter_data = await taxonomy_service.get_filter_taxonomy_data()
        return {"success": True, "data": filter_data}
    except Exception as e:
        logger.error(f"Filter taxonomy data error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/taxonomy/canonical-terms")
async def get_canonical_terms(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get a flat list of all canonical terms."""
    try:
        terms = await taxonomy_service.get_all_canonical_terms()
        return {"success": True, "terms": terms}
    except Exception as e:
        logger.error(f"Error getting canonical terms: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.get("/taxonomy/search")
async def search_taxonomy_terms(
    q: str,
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Search taxonomy terms."""
    try:
        terms = await taxonomy_service.search_terms(q)
        return {"success": True, "terms": terms}
    except Exception as e:
        logger.error(f"Taxonomy search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/taxonomy/stats")
async def get_taxonomy_statistics(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get taxonomy statistics."""
    try:
        stats = await taxonomy_service.get_statistics()
        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Taxonomy stats error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
