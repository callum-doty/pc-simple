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
