"""
API endpoint for dashboard metrics
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from database import get_db
from services.dashboard_service import DashboardService

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/dashboard", summary="Get all dashboard metrics", tags=["Dashboard"])
async def get_dashboard_data(db: Session = Depends(get_db)):
    """
    Retrieve a comprehensive set of metrics for the admin dashboard.
    """
    try:
        dashboard_service = DashboardService(db)
        data = await dashboard_service.get_dashboard_data()
        return data
    except Exception as e:
        logger.error(f"Error fetching dashboard data: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Internal server error while fetching dashboard data.",
        )


@router.get("/queue-health", summary="Get queue health metrics", tags=["Dashboard"])
async def get_queue_health(db: Session = Depends(get_db)):
    """
    Retrieve metrics about the document processing queue.
    """
    try:
        dashboard_service = DashboardService(db)
        data = await dashboard_service.get_queue_health_data()
        return data
    except Exception as e:
        logger.error(f"Error fetching queue health data: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Internal server error while fetching queue health data.",
        )


@router.get("/review-queue", summary="Get review queue breakdown by reason", tags=["Dashboard"])
async def get_review_queue(db: Session = Depends(get_db)):
    try:
        dashboard_service = DashboardService(db)
        return await dashboard_service.get_review_queue()
    except Exception as e:
        logger.error(f"Error fetching review queue: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.get("/data-quality", summary="Get confidence distributions and bad-data leaderboard", tags=["Dashboard"])
async def get_data_quality(db: Session = Depends(get_db)):
    try:
        dashboard_service = DashboardService(db)
        return await dashboard_service.get_data_quality()
    except Exception as e:
        logger.error(f"Error fetching data quality: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.get("/client-intelligence", summary="Get client volume and normalization analysis", tags=["Dashboard"])
async def get_client_intelligence(db: Session = Depends(get_db)):
    try:
        dashboard_service = DashboardService(db)
        return await dashboard_service.get_client_intelligence()
    except Exception as e:
        logger.error(f"Error fetching client intelligence: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.get("/geography", summary="Get document distribution by state", tags=["Dashboard"])
async def get_geography(db: Session = Depends(get_db)):
    try:
        dashboard_service = DashboardService(db)
        return await dashboard_service.get_geography()
    except Exception as e:
        logger.error(f"Error fetching geography: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.get("/frank-analysis", summary="Get franked mail analysis", tags=["Dashboard"])
async def get_frank_analysis(db: Session = Depends(get_db)):
    try:
        dashboard_service = DashboardService(db)
        return await dashboard_service.get_frank_analysis()
    except Exception as e:
        logger.error(f"Error fetching frank analysis: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.get("/filter-usage", summary="Get search filter adoption and zero-result stats", tags=["Dashboard"])
async def get_filter_usage(db: Session = Depends(get_db)):
    """
    Returns how often users apply each filter (client, state, date_year), the top values
    per filter, zero-result searches, and filter-only (no text query) search counts.
    """
    try:
        dashboard_service = DashboardService(db)
        return await dashboard_service.get_filter_usage()
    except Exception as e:
        logger.error(f"Error fetching filter usage: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.get("/temporal", summary="Get document timeline by date_created", tags=["Dashboard"])
async def get_temporal_analysis(db: Session = Depends(get_db)):
    try:
        dashboard_service = DashboardService(db)
        return await dashboard_service.get_temporal_analysis()
    except Exception as e:
        logger.error(f"Error fetching temporal analysis: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error.")


@router.get(
    "/incomplete-documents",
    summary="Get documents with missing data",
    tags=["Dashboard"],
)
async def get_incomplete_documents(db: Session = Depends(get_db)):
    """
    Retrieve documents that are missing critical data such as summary, extracted text,
    keywords, or embeddings. This is useful for identifying documents that failed during
    AI processing due to quota issues or other errors.
    """
    try:
        dashboard_service = DashboardService(db)
        data = await dashboard_service.get_incomplete_documents()
        return data
    except Exception as e:
        logger.error(f"Error fetching incomplete documents: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail="Internal server error while fetching incomplete documents.",
        )
