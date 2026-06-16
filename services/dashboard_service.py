"""
Dashboard service - handles calculating and aggregating dashboard metrics
"""

import logging
from sqlalchemy.orm import Session
from sqlalchemy import func, desc, case, Integer, text, cast, Float
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
        key_metrics = await self._get_key_metrics()
        trends = await self._get_trend_data()
        status_breakdown = await self._get_status_breakdown()
        recent_documents = await self._get_recent_documents()

        return {
            "core_processing": core_processing_metrics,
            "ai_analysis": ai_analysis_metrics,
            "user_engagement": user_engagement_metrics,
            "key_metrics": key_metrics,
            "trends": trends,
            "status_breakdown": status_breakdown,
            "recent_documents": recent_documents,
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

    async def get_incomplete_documents(self) -> dict:
        """
        Identifies documents that are missing critical data (summary, extracted text, keywords, embeddings).
        This is particularly useful for identifying documents that failed during AI processing due to quota issues.
        """
        try:
            # Find documents missing summary (in ai_analysis).
            # Includes FAILED docs and COMPLETED docs where AI silently produced
            # placeholder values ("No summary available") instead of real content.
            incomplete_statuses = ["COMPLETED", "FAILED"]
            missing_summary = (
                self.db.query(Document)
                .filter(Document.status.in_(incomplete_statuses))
                .filter(
                    (Document.ai_analysis.is_(None))
                    | (Document.ai_analysis["summary"].astext.is_(None))
                    | (Document.ai_analysis["summary"].astext == "")
                    | (Document.ai_analysis["summary"].astext == "No summary available")
                    | (Document.ai_analysis["error"].astext.isnot(None))
                )
                .order_by(desc(Document.created_at))
                .limit(100)
                .all()
            )

            # Find documents missing extracted text
            missing_text = (
                self.db.query(Document)
                .filter(Document.status.in_(incomplete_statuses))
                .filter(
                    (Document.extracted_text.is_(None))
                    | (Document.extracted_text == "")
                )
                .order_by(desc(Document.created_at))
                .limit(100)
                .all()
            )

            # Find documents missing keywords — NULL or where AI analysis failed
            # (error key present means analysis was never actually completed).
            missing_keywords = (
                self.db.query(Document)
                .filter(Document.status.in_(incomplete_statuses))
                .filter(
                    (Document.keywords.is_(None))
                    | (Document.ai_analysis["error"].astext.isnot(None))
                )
                .order_by(desc(Document.created_at))
                .limit(100)
                .all()
            )

            # Find documents missing embeddings
            missing_embeddings = (
                self.db.query(Document)
                .filter(Document.status.in_(incomplete_statuses))
                .filter(Document.search_vector.is_(None))
                .order_by(desc(Document.created_at))
                .limit(100)
                .all()
            )

            # Helper function to convert document to dict
            def doc_to_dict(doc):
                return {
                    "id": doc.id,
                    "filename": doc.filename,
                    "status": doc.status,
                    "created_at": (
                        doc.created_at.isoformat() if doc.created_at else None
                    ),
                    "processed_at": (
                        doc.processed_at.isoformat() if doc.processed_at else None
                    ),
                    "processing_error": doc.processing_error,
                }

            return {
                "summary": {
                    "count": len(missing_summary),
                    "documents": [doc_to_dict(doc) for doc in missing_summary],
                },
                "extracted_text": {
                    "count": len(missing_text),
                    "documents": [doc_to_dict(doc) for doc in missing_text],
                },
                "keywords": {
                    "count": len(missing_keywords),
                    "documents": [doc_to_dict(doc) for doc in missing_keywords],
                },
                "embeddings": {
                    "count": len(missing_embeddings),
                    "documents": [doc_to_dict(doc) for doc in missing_embeddings],
                },
                "total_unique_incomplete": len(
                    set(
                        [doc.id for doc in missing_summary]
                        + [doc.id for doc in missing_text]
                        + [doc.id for doc in missing_keywords]
                        + [doc.id for doc in missing_embeddings]
                    )
                ),
            }
        except Exception as e:
            logger.error(f"Error getting incomplete documents: {e}", exc_info=True)
            return {
                "summary": {"count": 0, "documents": []},
                "extracted_text": {"count": 0, "documents": []},
                "keywords": {"count": 0, "documents": []},
                "embeddings": {"count": 0, "documents": []},
                "total_unique_incomplete": 0,
            }

    async def _get_key_metrics(self) -> dict:
        """Calculate key dashboard metrics."""
        try:
            # Total documents
            total_docs = self.db.query(func.count(Document.id)).scalar() or 0

            # Success rate (7 days)
            seven_days_ago = datetime.utcnow() - timedelta(days=7)
            total_processed_7d = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.processed_at >= seven_days_ago,
                    Document.status.in_(
                        [DocumentStatus.COMPLETED, DocumentStatus.FAILED]
                    ),
                )
                .scalar()
                or 0
            )

            successful_7d = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.processed_at >= seven_days_ago,
                    Document.status == DocumentStatus.COMPLETED,
                )
                .scalar()
                or 0
            )

            success_rate_7d = (
                round((successful_7d / total_processed_7d * 100), 2)
                if total_processed_7d > 0
                else 100.0
            )

            # Average processing time
            avg_processing_time = self.db.query(
                func.avg(Document.processed_at - Document.created_at)
            ).filter(
                Document.status == DocumentStatus.COMPLETED,
                Document.processed_at.isnot(None),
            ).scalar() or timedelta(
                seconds=0
            )

            # Queue depth
            queue_depth = (
                self.db.query(func.count(Document.id))
                .filter(Document.status == DocumentStatus.PENDING)
                .scalar()
                or 0
            )

            return {
                "total_documents": total_docs,
                "success_rate_7d": success_rate_7d,
                "avg_processing_time_seconds": round(
                    avg_processing_time.total_seconds(), 2
                ),
                "queue_depth": queue_depth,
            }
        except Exception as e:
            logger.error(f"Error calculating key metrics: {e}")
            return {
                "total_documents": 0,
                "success_rate_7d": 0,
                "avg_processing_time_seconds": 0,
                "queue_depth": 0,
            }

    async def _get_trend_data(self) -> dict:
        """Calculate 30-day trend data for uploads, completions, and searches."""
        try:
            thirty_days_ago = datetime.utcnow() - timedelta(days=30)

            # Daily uploads
            daily_uploads = (
                self.db.query(
                    func.date(Document.created_at).label("date"),
                    func.count(Document.id).label("count"),
                )
                .filter(Document.created_at >= thirty_days_ago)
                .group_by(func.date(Document.created_at))
                .order_by(func.date(Document.created_at))
                .all()
            )

            # Daily completions
            daily_completions = (
                self.db.query(
                    func.date(Document.processed_at).label("date"),
                    func.count(Document.id).label("count"),
                )
                .filter(
                    Document.processed_at >= thirty_days_ago,
                    Document.status == DocumentStatus.COMPLETED,
                )
                .group_by(func.date(Document.processed_at))
                .order_by(func.date(Document.processed_at))
                .all()
            )

            # Daily searches
            daily_searches = (
                self.db.query(
                    func.date(SearchQuery.timestamp).label("date"),
                    func.count(SearchQuery.id).label("count"),
                )
                .filter(SearchQuery.timestamp >= thirty_days_ago)
                .group_by(func.date(SearchQuery.timestamp))
                .order_by(func.date(SearchQuery.timestamp))
                .all()
            )

            # Convert to lists of dicts
            uploads_data = [
                {"date": str(row.date), "count": row.count} for row in daily_uploads
            ]
            completions_data = [
                {"date": str(row.date), "count": row.count} for row in daily_completions
            ]
            searches_data = [
                {"date": str(row.date), "count": row.count} for row in daily_searches
            ]

            return {
                "daily_uploads": uploads_data,
                "daily_completions": completions_data,
                "daily_searches": searches_data,
            }
        except Exception as e:
            logger.error(f"Error calculating trend data: {e}")
            return {
                "daily_uploads": [],
                "daily_completions": [],
                "daily_searches": [],
            }

    async def _get_status_breakdown(self) -> dict:
        """Get document status breakdown."""
        try:
            status_counts = (
                self.db.query(Document.status, func.count(Document.status))
                .group_by(Document.status)
                .all()
            )

            status_map = dict(status_counts)

            return {
                "pending": status_map.get(DocumentStatus.PENDING, 0),
                "processing": status_map.get(DocumentStatus.PROCESSING, 0),
                "completed": status_map.get(DocumentStatus.COMPLETED, 0),
                "failed": status_map.get(DocumentStatus.FAILED, 0),
            }
        except Exception as e:
            logger.error(f"Error calculating status breakdown: {e}")
            return {"pending": 0, "processing": 0, "completed": 0, "failed": 0}

    async def _get_recent_documents(self) -> list:
        """Get the 10 most recent documents."""
        try:
            recent_docs = (
                self.db.query(Document)
                .order_by(desc(Document.created_at))
                .limit(10)
                .all()
            )

            return [
                {
                    "id": doc.id,
                    "filename": doc.filename,
                    "status": doc.status,
                    "created_at": doc.created_at.isoformat() if doc.created_at else None,
                    "processed_at": (
                        doc.processed_at.isoformat() if doc.processed_at else None
                    ),
                }
                for doc in recent_docs
            ]
        except Exception as e:
            logger.error(f"Error getting recent documents: {e}")
            return []

    # -------------------------------------------------------------------------
    # Intelligence analytics methods
    # -------------------------------------------------------------------------

    async def get_review_queue(self) -> dict:
        """Breakdown of documents needing review, by reason."""
        try:
            needs_review_flagged = (
                self.db.query(func.count(Document.id))
                .filter(Document.needs_review == True)
                .scalar() or 0
            )
            missing_embeddings = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.search_vector.is_(None),
                )
                .scalar() or 0
            )
            missing_text = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    (Document.extracted_text.is_(None)) | (Document.extracted_text == ""),
                )
                .scalar() or 0
            )
            missing_keywords = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords.is_(None),
                )
                .scalar() or 0
            )
            has_errors = (
                self.db.query(func.count(Document.id))
                .filter(Document.processing_error.isnot(None))
                .scalar() or 0
            )
            low_date_conf = (
                self.db.query(func.count(Document.id))
                .filter(Document.date_confidence == "LOW")
                .scalar() or 0
            )
            low_client_conf = (
                self.db.query(func.count(Document.id))
                .filter(Document.client_confidence == "LOW")
                .scalar() or 0
            )
            low_state_conf = (
                self.db.query(func.count(Document.id))
                .filter(Document.state_confidence == "LOW")
                .scalar() or 0
            )
            return {
                "needs_review_flagged": needs_review_flagged,
                "missing_embeddings": missing_embeddings,
                "missing_text": missing_text,
                "missing_keywords": missing_keywords,
                "has_errors": has_errors,
                "low_date_confidence": low_date_conf,
                "low_client_confidence": low_client_conf,
                "low_state_confidence": low_state_conf,
            }
        except Exception as e:
            logger.error(f"Error getting review queue: {e}")
            return {}

    async def get_data_quality(self) -> dict:
        """Confidence distributions and bad-data leaderboard."""
        _CONF_SCORE = {"high": 1.0, "medium": 0.5, "low": 0.0}

        def _dist(column):
            rows = (
                self.db.query(column, func.count(Document.id))
                .filter(column.isnot(None))
                .group_by(column)
                .all()
            )
            return {level: cnt for level, cnt in rows}

        try:
            date_dist = _dist(Document.date_confidence)
            client_dist = _dist(Document.client_confidence)
            state_dist = _dist(Document.state_confidence)

            # Composite quality score per client_canonical
            # Pull raw counts per (client_canonical, each confidence level)
            rows = self.db.execute(text("""
                SELECT
                    client_canonical,
                    COUNT(*) AS doc_count,
                    AVG(
                        CASE date_confidence   WHEN 'HIGH' THEN 1.0 WHEN 'MEDIUM' THEN 0.5 ELSE 0.0 END +
                        CASE client_confidence WHEN 'HIGH' THEN 1.0 WHEN 'MEDIUM' THEN 0.5 ELSE 0.0 END +
                        CASE state_confidence  WHEN 'HIGH' THEN 1.0 WHEN 'MEDIUM' THEN 0.5 ELSE 0.0 END
                    ) / 3.0 AS avg_quality
                FROM documents
                WHERE client_canonical IS NOT NULL
                GROUP BY client_canonical
                HAVING COUNT(*) >= 3
                ORDER BY avg_quality ASC
                LIMIT 15
            """)).fetchall()

            bad_data_leaderboard = [
                {
                    "client": r[0],
                    "doc_count": r[1],
                    "avg_quality": round(float(r[2]), 3) if r[2] is not None else 0.0,
                }
                for r in rows
            ]

            return {
                "date_confidence": date_dist,
                "client_confidence": client_dist,
                "state_confidence": state_dist,
                "bad_data_leaderboard": bad_data_leaderboard,
            }
        except Exception as e:
            logger.error(f"Error getting data quality: {e}")
            return {}

    async def get_client_intelligence(self) -> dict:
        """Top clients by volume and dirty-client (normalization) analysis."""
        try:
            top_clients = (
                self.db.query(Document.client_canonical, func.count(Document.id).label("doc_count"))
                .filter(Document.client_canonical.isnot(None))
                .group_by(Document.client_canonical)
                .order_by(desc("doc_count"))
                .limit(20)
                .all()
            )

            dirty_clients = self.db.execute(text("""
                SELECT
                    client_canonical,
                    COUNT(DISTINCT client) AS variants,
                    COUNT(*) AS doc_count
                FROM documents
                WHERE client_canonical IS NOT NULL AND client IS NOT NULL
                GROUP BY client_canonical
                HAVING COUNT(DISTINCT client) > 1
                ORDER BY variants DESC
                LIMIT 20
            """)).fetchall()

            return {
                "top_clients": [
                    {"client": r[0], "doc_count": r[1]} for r in top_clients
                ],
                "dirty_clients": [
                    {"client": r[0], "variants": r[1], "doc_count": r[2]}
                    for r in dirty_clients
                ],
            }
        except Exception as e:
            logger.error(f"Error getting client intelligence: {e}")
            return {}

    async def get_geography(self) -> dict:
        """Document distribution by state, with percentage of total."""
        try:
            rows = self.db.execute(text("""
                SELECT
                    TRIM(state) AS state,
                    COUNT(*) AS doc_count,
                    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER (), 1) AS pct
                FROM documents
                WHERE state IS NOT NULL AND TRIM(state) != ''
                GROUP BY TRIM(state)
                ORDER BY doc_count DESC
            """)).fetchall()

            frank_by_state = self.db.execute(text("""
                SELECT
                    TRIM(state) AS state,
                    COUNT(*) FILTER (WHERE is_frank = true) AS frank_count,
                    COUNT(*) AS total
                FROM documents
                WHERE state IS NOT NULL AND TRIM(state) != ''
                GROUP BY TRIM(state)
                ORDER BY frank_count DESC
            """)).fetchall()

            return {
                "by_state": [
                    {"state": r[0], "doc_count": r[1], "pct": float(r[2])}
                    for r in rows
                ],
                "frank_by_state": [
                    {"state": r[0], "frank_count": r[1], "total": r[2]}
                    for r in frank_by_state
                ],
            }
        except Exception as e:
            logger.error(f"Error getting geography: {e}")
            return {}

    async def get_frank_analysis(self) -> dict:
        """Franked mail ratio overall, by state, by client, and over time."""
        try:
            totals = self.db.execute(text("""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE is_frank = true) AS frank_count
                FROM documents
            """)).fetchone()

            by_client = self.db.execute(text("""
                SELECT
                    client_canonical,
                    COUNT(*) FILTER (WHERE is_frank = true) AS frank_count,
                    COUNT(*) AS total
                FROM documents
                WHERE client_canonical IS NOT NULL
                GROUP BY client_canonical
                HAVING COUNT(*) FILTER (WHERE is_frank = true) > 0
                ORDER BY frank_count DESC
                LIMIT 15
            """)).fetchall()

            over_time = self.db.execute(text("""
                SELECT
                    DATE_TRUNC('month', date_created)::date AS month,
                    COUNT(*) FILTER (WHERE is_frank = true) AS frank_count,
                    COUNT(*) AS total
                FROM documents
                WHERE date_created IS NOT NULL
                GROUP BY DATE_TRUNC('month', date_created)
                ORDER BY month
            """)).fetchall()

            return {
                "total": totals[0] if totals else 0,
                "frank_count": totals[1] if totals else 0,
                "frank_pct": round(totals[1] / totals[0] * 100, 1) if totals and totals[0] else 0,
                "by_client": [
                    {"client": r[0], "frank_count": r[1], "total": r[2]}
                    for r in by_client
                ],
                "over_time": [
                    {"month": str(r[0]), "frank_count": r[1], "total": r[2]}
                    for r in over_time
                ],
            }
        except Exception as e:
            logger.error(f"Error getting frank analysis: {e}")
            return {}

    async def get_temporal_analysis(self) -> dict:
        """Document timeline using date_created (the document's actual date, not upload date)."""
        try:
            monthly = self.db.execute(text("""
                SELECT
                    DATE_TRUNC('month', date_created)::date AS month,
                    COUNT(*) AS doc_count
                FROM documents
                WHERE date_created IS NOT NULL
                GROUP BY DATE_TRUNC('month', date_created)
                ORDER BY month
            """)).fetchall()

            top_days = self.db.execute(text("""
                SELECT date_created, COUNT(*) AS doc_count
                FROM documents
                WHERE date_created IS NOT NULL
                GROUP BY date_created
                ORDER BY doc_count DESC
                LIMIT 10
            """)).fetchall()

            lag = self.db.execute(text("""
                SELECT
                    AVG(
                        EXTRACT(EPOCH FROM (created_at - date_created::timestamp with time zone)) / 86400.0
                    ) AS avg_lag_days
                FROM documents
                WHERE date_created IS NOT NULL AND created_at IS NOT NULL
            """)).fetchone()

            return {
                "monthly": [
                    {"month": str(r[0]), "doc_count": r[1]} for r in monthly
                ],
                "top_spike_days": [
                    {"date": str(r[0]), "doc_count": r[1]} for r in top_days
                ],
                "avg_lag_days": round(float(lag[0]), 1) if lag and lag[0] is not None else None,
            }
        except Exception as e:
            logger.error(f"Error getting temporal analysis: {e}")
            return {}
