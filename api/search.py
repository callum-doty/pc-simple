"""
Search API routes — moved from main.py as part of FIX-006.

Handles:
  GET /api/documents/search
  GET /api/search/canonical/{canonical_term}
  GET /api/search/verbatim/{verbatim_term}
  GET /api/search/top-queries
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from api.dependencies import get_search_service, limiter
from services.search_service import SearchService
from services.security_service import security_service

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/documents/search")
@limiter.limit("30/minute")
async def search_documents(
    request: Request,
    q: str = "",
    page: int = 1,
    per_page: int = 20,
    primary_category: Optional[str] = None,
    subcategory: Optional[str] = None,
    canonical_term: Optional[str] = None,
    client_canonical: Optional[str] = None,
    state: Optional[str] = None,
    date_year: Optional[int] = None,
    sort_by: str = "relevance",
    sort_direction: str = "desc",
    include_facets: bool = True,
    search_service: SearchService = Depends(get_search_service),
):
    """Search documents — public endpoint.

    Args:
        include_facets: Set to false to skip expensive facet generation for
                        faster initial page load.
    """
    try:
        safe_query = security_service.validate_search_query(q)

        if page < 1:
            page = 1
        if per_page < 1 or per_page > 100:
            per_page = 20

        allowed_sort_fields = [
            "relevance", "created_at", "updated_at", "filename", "file_size",
        ]
        if sort_by not in allowed_sort_fields:
            sort_by = "relevance"
        if sort_direction.lower() not in ["asc", "desc"]:
            sort_direction = "desc"

        results = await search_service.search(
            query=safe_query,
            page=page,
            per_page=per_page,
            primary_category=primary_category,
            subcategory=subcategory,
            canonical_term=canonical_term,
            client_canonical=client_canonical,
            state=state,
            date_year=date_year,
            sort_by=sort_by,
            sort_direction=sort_direction,
            include_facets=include_facets,
        )

        return {
            "success": True,
            "documents": results["documents"],
            "pagination": results["pagination"],
            "facets": results["facets"],
            "total_count": results["total_count"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search/canonical/{canonical_term}")
async def search_by_canonical_term(
    canonical_term: str,
    q: str = "",
    search_service: SearchService = Depends(get_search_service),
):
    """Search documents by canonical term with optional query."""
    try:
        results = await search_service.search_by_canonical_term(canonical_term, query=q)
        return {"success": True, "documents": results}
    except Exception as e:
        logger.error(f"Canonical term search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search/verbatim/{verbatim_term}")
async def search_by_verbatim_term(
    verbatim_term: str,
    q: str = "",
    search_service: SearchService = Depends(get_search_service),
):
    """Search documents by verbatim term with optional query."""
    try:
        results = await search_service.search_by_verbatim_term(verbatim_term, query=q)
        return {"success": True, "documents": results}
    except Exception as e:
        logger.error(f"Verbatim term search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/search/top-queries")
async def get_top_queries(
    search_service: SearchService = Depends(get_search_service),
):
    """Get top 8 search queries."""
    try:
        queries = await search_service.get_top_queries(limit=8)
        return {"success": True, "queries": queries}
    except Exception as e:
        logger.error(f"Top queries error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
