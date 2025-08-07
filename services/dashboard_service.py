"""
Dashboard service - handles calculating and aggregating dashboard metrics
"""

import logging
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, case, Integer
from datetime import datetime, timedelta

from models.document import Document, DocumentStatus
from models.search_query import SearchQuery
from services.document_service import DocumentService
from services.search_service import SearchService

logger = logging.getLogger(__name__)


class DashboardService:
    """Service for calculating dashboard metrics"""

    def __init__(self, db: Session):
        self.db = db
        self.document_service = DocumentService(db)
        self.search_service = SearchService(
            db, None
        )  # Preview service not needed for metrics

    async def get_dashboard_data(self) -> dict:
        """
        Gathers all data for the admin dashboard.
        """

        core_processing_metrics = await self._get_core_processing_metrics()
        ai_analysis_metrics = await self._get_ai_analysis_metrics()
        user_engagement_metrics = await self._get_user_engagement_metrics()

        return {
            "core_processing": core_processing_metrics,
            "ai_analysis": ai_analysis_metrics,
            "user_engagement": user_engagement_metrics,
        }

    async def _get_core_processing_metrics(self) -> dict:
        """Calculate core processing metrics."""
        try:
            total_processed = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status.in_(
                        [DocumentStatus.COMPLETED, DocumentStatus.FAILED]
                    )
                )
                .scalar()
                or 0
            )

            successful_docs = (
                self.db.query(func.count(Document.id))
                .filter(Document.status == DocumentStatus.COMPLETED)
                .scalar()
                or 0
            )

            success_rate = (
                (successful_docs / total_processed * 100)
                if total_processed > 0
                else 100
            )

            avg_processing_time = self.db.query(
                func.avg(Document.processed_at - Document.created_at)
            ).filter(
                Document.status == DocumentStatus.COMPLETED,
                Document.processed_at.isnot(None),
            ).scalar() or timedelta(
                seconds=0
            )

            queue_depth = (
                self.db.query(func.count(Document.id))
                .filter(Document.status == DocumentStatus.PENDING)
                .scalar()
                or 0
            )

            one_day_ago = datetime.utcnow() - timedelta(days=1)
            throughput_24h = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.processed_at >= one_day_ago,
                )
                .scalar()
                or 0
            )

            return {
                "processing_success_rate": round(success_rate, 2),
                "average_processing_time_seconds": avg_processing_time.total_seconds(),
                "queue_depth": queue_depth,
                "processing_throughput_24h": throughput_24h,
            }
        except Exception as e:
            logger.error(f"Error calculating core processing metrics: {e}")
            return {}

    async def _get_ai_analysis_metrics(self) -> dict:
        """Calculate AI analysis quality metrics."""
        try:
            completed_docs = (
                self.db.query(func.count(Document.id))
                .filter(Document.status == DocumentStatus.COMPLETED)
                .scalar()
                or 0
            )

            if completed_docs == 0:
                return {
                    "analysis_completion_rate": 0,
                    "keyword_mapping_success_rate": 0,
                    "embedding_generation_rate": 0,
                }

            with_analysis = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.ai_analysis.isnot(None),
                )
                .scalar()
                or 0
            )

            with_mappings = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords["mapping_count"].astext.cast(Integer) > 0,
                )
                .scalar()
                or 0
            )

            with_embeddings = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.search_vector.isnot(None),
                )
                .scalar()
                or 0
            )

            analysis_completion_rate = with_analysis / completed_docs * 100
            keyword_mapping_rate = with_mappings / completed_docs * 100
            embedding_generation_rate = with_embeddings / completed_docs * 100

            return {
                "analysis_completion_rate": round(analysis_completion_rate, 2),
                "keyword_mapping_success_rate": round(keyword_mapping_rate, 2),
                "embedding_generation_rate": round(embedding_generation_rate, 2),
            }
        except Exception as e:
            logger.error(f"Error calculating AI analysis metrics: {e}")
            return {}

    async def _get_user_engagement_metrics(self) -> dict:
        """Calculate user engagement metrics."""
        try:
            one_week_ago = datetime.utcnow() - timedelta(days=7)

            search_query_volume_7d = (
                self.db.query(func.count(SearchQuery.id))
                .filter(SearchQuery.timestamp >= one_week_ago)
                .scalar()
                or 0
            )

            top_queries = await self.search_service.get_top_queries(limit=10)

            upload_volume_7d = (
                self.db.query(func.count(Document.id))
                .filter(Document.created_at >= one_week_ago)
                .scalar()
                or 0
            )

            return {
                "search_query_volume_7d": search_query_volume_7d,
                "top_search_terms": top_queries,
                "upload_volume_7d": upload_volume_7d,
            }
        except Exception as e:
            logger.error(f"Error calculating user engagement metrics: {e}")
            return {}

    async def get_queue_health_data(self) -> dict:
        """
        Gathers all data for the queue health dashboard.
        """
        try:
            status_counts = (
                self.db.query(Document.status, func.count(Document.status))
                .group_by(Document.status)
                .all()
            )

            # Convert to a dictionary for easier access
            status_map = dict(status_counts)

            # Get counts for each status, defaulting to 0
            queued_count = status_map.get(DocumentStatus.QUEUED, 0)
            pending_count = status_map.get(DocumentStatus.PENDING, 0)
            processing_count = status_map.get(DocumentStatus.PROCESSING, 0)
            failed_count = status_map.get(DocumentStatus.FAILED, 0)
            completed_count = status_map.get(DocumentStatus.COMPLETED, 0)

            # Get the oldest queued document
            oldest_queued_doc = (
                self.db.query(Document)
                .filter(Document.status == DocumentStatus.QUEUED)
                .order_by(Document.created_at.asc())
                .first()
            )

            oldest_queued_time = None
            if oldest_queued_doc:
                oldest_queued_time = (
                    datetime.utcnow() - oldest_queued_doc.created_at
                ).total_seconds()

            return {
                "queued": queued_count,
                "pending": pending_count,
                "processing": processing_count,
                "failed": failed_count,
                "completed": completed_count,
                "total": sum(
                    [
                        queued_count,
                        pending_count,
                        processing_count,
                        failed_count,
                        completed_count,
                    ]
                ),
                "oldest_queued_seconds": oldest_queued_time,
            }
        except Exception as e:
            logger.error(f"Error calculating queue health data: {e}")
            return {}
