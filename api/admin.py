"""
Admin, AI, stats, and task management API routes — moved from main.py as part of FIX-006.

Handles:
  POST /api/admin/clear-cache
  POST /api/admin/backfill-features
  GET  /api/ai/info
  GET  /api/ai/analysis-types
  POST /api/documents/{document_id}/analyze
  GET  /api/tasks/{task_id}/status
  GET  /api/stats
  GET  /api/stats/mappings
  GET  /api/documents/{document_id}/mappings
"""

import logging
import redis as redis_lib

from fastapi import APIRouter, Depends, Form, HTTPException
from celery.result import AsyncResult
from sqlalchemy.orm import Session

from api.dependencies import (
    get_ai_service,
    get_document_service,
    get_search_service,
    get_storage_service,
)
from config import get_settings
from database import get_db
from models.document import Document
from services.ai_service import AIService
from services.document_service import DocumentService
from services.search_service import SearchService
from services.storage_service import StorageService

logger = logging.getLogger(__name__)
settings = get_settings()

router = APIRouter()


@router.post("/admin/clear-cache")
async def clear_redis_cache(password: str = Form(...)):
    """Clear Redis search and facet caches — admin only."""
    try:
        admin_password = settings.upload_password or "upload123"
        if password != admin_password:
            raise HTTPException(status_code=401, detail="Invalid password")

        if not settings.redis_url:
            raise HTTPException(status_code=500, detail="Redis not configured")

        r = redis_lib.from_url(settings.redis_url, decode_responses=True)
        r.ping()

        search_keys = list(r.scan_iter("search:*"))
        facet_keys = list(r.scan_iter("facets:*"))
        all_keys = search_keys + facet_keys

        if not all_keys:
            return {
                "success": True,
                "message": "Cache already empty",
                "deleted_count": 0,
                "search_keys": 0,
                "facet_keys": 0,
            }

        deleted = r.delete(*all_keys)
        logger.info(f"Cleared {deleted} cache keys from Redis")

        return {
            "success": True,
            "message": f"Successfully cleared {deleted} cache entries",
            "deleted_count": deleted,
            "search_keys": len(search_keys),
            "facet_keys": len(facet_keys),
            "use_direct_urls": settings.use_direct_urls,
            "storage_type": settings.storage_type,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error clearing cache: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/admin/backfill-features")
async def backfill_feature_extraction(
    password: str = Form(...), db: Session = Depends(get_db)
):
    """
    Enqueue feature extraction for all COMPLETED documents missing client_canonical.
    Reads from already-stored extracted_text and ai_analysis — no AI reprocessing.
    """
    from worker import extract_document_features_task

    admin_password = settings.upload_password or "upload123"
    if password != admin_password:
        raise HTTPException(status_code=401, detail="Invalid password")

    pending = (
        db.query(Document.id)
        .filter(Document.status == "COMPLETED")
        .filter(Document.client_canonical.is_(None))
        .filter(Document.extracted_text.isnot(None))
        .all()
    )
    doc_ids = [row[0] for row in pending]

    for doc_id in doc_ids:
        extract_document_features_task.delay(doc_id)

    logger.info(f"Backfill: enqueued feature extraction for {len(doc_ids)} documents.")
    return {
        "success": True,
        "enqueued": len(doc_ids),
        "message": (
            f"Enqueued feature extraction for {len(doc_ids)} documents. "
            f"Check worker logs for progress."
        ),
    }


@router.get("/ai/info")
async def get_ai_info(ai_service: AIService = Depends(get_ai_service)):
    """Get AI service configuration and capabilities."""
    try:
        info = ai_service.get_ai_info()
        return {"success": True, "ai_info": info}
    except Exception as e:
        logger.error(f"AI info error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/ai/analysis-types")
async def get_analysis_types(ai_service: AIService = Depends(get_ai_service)):
    """Get available analysis types."""
    try:
        types = ai_service.get_available_analysis_types()
        return {"success": True, "analysis_types": types}
    except Exception as e:
        logger.error(f"Analysis types error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/documents/{document_id}/analyze")
async def analyze_document_with_type(
    document_id: int,
    analysis_type: str = "unified",
    ai_service: AIService = Depends(get_ai_service),
    document_service: DocumentService = Depends(get_document_service),
    storage_service: StorageService = Depends(get_storage_service),
):
    """Perform immediate analysis on a document with specified type."""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        result = await ai_service.analyze_document(
            document.file_path, document.filename, analysis_type
        )
        return {
            "success": True,
            "document_id": document_id,
            "analysis_type": analysis_type,
            "result": result,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Document analysis error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tasks/{task_id}/status")
async def get_task_status(task_id: str):
    """Get the status of a Celery task."""
    task_result = AsyncResult(task_id)
    return {
        "success": True,
        "task": {
            "task_id": task_id,
            "status": task_result.status,
            "result": task_result.result,
        },
    }


@router.get("/stats")
async def get_statistics(
    document_service: DocumentService = Depends(get_document_service),
):
    """Get application statistics."""
    try:
        stats = await document_service.get_statistics()
        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Stats error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats/mappings")
async def get_mapping_stats(
    search_service: SearchService = Depends(get_search_service),
):
    """Get statistics about keyword mappings."""
    try:
        stats = await search_service.get_mapping_statistics()
        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Mapping stats error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/documents/{document_id}/mappings")
async def get_document_mappings(
    document_id: int,
    document_service: DocumentService = Depends(get_document_service),
):
    """Get keyword mappings for a document."""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        mappings = document.get_keyword_mappings()
        return {"success": True, "mappings": mappings}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Document mappings error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
