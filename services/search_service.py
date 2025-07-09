"""
Search service - handles document search and filtering
Simplified search implementation with text-based and category filtering
"""

import logging
from typing import Dict, Any, List, Optional
from sqlalchemy import or_, and_, func, desc
from sqlalchemy.orm import Session

from database import SessionLocal
from models.document import Document, DocumentStatus
from models.taxonomy import TaxonomyTerm
from config import get_settings
from services.preview_service import PreviewService

logger = logging.getLogger(__name__)
settings = get_settings()


class SearchService:
    """Service for searching and filtering documents"""

    def __init__(self):
        self.db = SessionLocal()
        self.preview_service = PreviewService()

    async def search(
        self,
        query: str = "",
        page: int = 1,
        per_page: int = 20,
        category: Optional[str] = None,
        status: Optional[str] = None,
        sort_by: str = "created_at",
        sort_direction: str = "desc",
    ) -> Dict[str, Any]:
        """
        Search documents with filtering and pagination
        Returns documents, pagination info, and facets
        """
        try:
            # Build base query
            base_query = self.db.query(Document).filter(
                Document.status == DocumentStatus.COMPLETED
            )

            # Apply text search
            if query.strip():
                search_term = f"%{query.strip()}%"
                base_query = base_query.filter(
                    or_(
                        Document.filename.ilike(search_term),
                        Document.search_content.ilike(search_term),
                        Document.extracted_text.ilike(search_term),
                    )
                )

            # Apply category filter
            if category:
                # Search in keywords JSON field
                base_query = base_query.filter(
                    Document.keywords.op("->>")("categories").op("?")(category)
                )

            # Apply status filter
            if status:
                base_query = base_query.filter(Document.status == status)

            # Get total count for pagination
            total_count = base_query.count()

            # Apply sorting
            if sort_direction.lower() == "desc":
                base_query = base_query.order_by(desc(getattr(Document, sort_by)))
            else:
                base_query = base_query.order_by(getattr(Document, sort_by))

            # Apply pagination
            offset = (page - 1) * per_page
            documents = base_query.offset(offset).limit(per_page).all()

            # Format documents for response
            formatted_docs = []
            for doc in documents:
                # Generate preview URL dynamically
                preview_url = self.preview_service.get_preview_url(doc.file_path)

                formatted_doc = {
                    "id": doc.id,
                    "filename": doc.filename,
                    "file_size": doc.file_size,
                    "status": doc.status,
                    "created_at": (
                        doc.created_at.isoformat() if doc.created_at else None
                    ),
                    "summary": doc.get_summary(),
                    "categories": doc.get_categories(),
                    "keywords": doc.get_keyword_list(),
                    "preview_url": preview_url,
                    "file_type": doc.get_metadata("file_type", "unknown"),
                    "document_type": (
                        doc.ai_analysis.get("document_analysis", {}).get(
                            "document_type"
                        )
                        if doc.ai_analysis
                        else None
                    ),
                    "campaign_type": (
                        doc.ai_analysis.get("document_analysis", {}).get(
                            "campaign_type"
                        )
                        if doc.ai_analysis
                        else None
                    ),
                    "document_tone": (
                        doc.ai_analysis.get("document_analysis", {}).get(
                            "document_tone"
                        )
                        if doc.ai_analysis
                        else None
                    ),
                }
                formatted_docs.append(formatted_doc)

            # Generate pagination info
            pagination = self._create_pagination_info(page, per_page, total_count)

            # Generate facets (categories for filtering)
            facets = await self._generate_facets()

            return {
                "documents": formatted_docs,
                "pagination": pagination,
                "facets": facets,
                "total_count": total_count,
                "query": query,
            }

        except Exception as e:
            logger.error(f"Error in search: {str(e)}")
            return {
                "documents": [],
                "pagination": {"page": 1, "per_page": per_page, "total": 0, "pages": 0},
                "facets": {"categories": []},
                "total_count": 0,
                "query": query,
                "error": str(e),
            }

    def _create_pagination_info(
        self, page: int, per_page: int, total_count: int
    ) -> Dict[str, Any]:
        """Create pagination information"""
        total_pages = (total_count + per_page - 1) // per_page if per_page > 0 else 0

        return {
            "page": page,
            "per_page": per_page,
            "total": total_count,
            "pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
            "prev_page": page - 1 if page > 1 else None,
            "next_page": page + 1 if page < total_pages else None,
        }

    async def _generate_facets(self) -> Dict[str, Any]:
        """Generate facets for filtering using taxonomy"""
        try:
            # Get taxonomy-based categories
            taxonomy_categories = (
                self.db.query(TaxonomyTerm.primary_category)
                .distinct()
                .order_by(TaxonomyTerm.primary_category)
                .all()
            )

            # Get document categories and count them
            docs_with_keywords = (
                self.db.query(Document)
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords.isnot(None),
                )
                .all()
            )

            category_counts = {}
            for doc in docs_with_keywords:
                doc_categories = doc.get_categories()
                for category in doc_categories:
                    category_counts[category] = category_counts.get(category, 0) + 1

            # Build facets with taxonomy structure
            facets = {
                "categories": [],
                "primary_categories": [],
                "subcategories": {},
            }

            # Add primary categories from taxonomy
            for (primary_category,) in taxonomy_categories:
                count = category_counts.get(primary_category, 0)
                facets["primary_categories"].append(
                    {"name": primary_category, "count": count}
                )

                # Get subcategories for this primary category
                subcategories = (
                    self.db.query(TaxonomyTerm.subcategory)
                    .filter(
                        TaxonomyTerm.primary_category == primary_category,
                        TaxonomyTerm.subcategory.isnot(None),
                    )
                    .distinct()
                    .all()
                )

                facets["subcategories"][primary_category] = []
                for (subcategory,) in subcategories:
                    if subcategory:
                        sub_count = category_counts.get(subcategory, 0)
                        facets["subcategories"][primary_category].append(
                            {"name": subcategory, "count": sub_count}
                        )

            # Legacy categories list for backward compatibility
            all_categories = sorted(category_counts.keys())
            facets["categories"] = [
                {"name": cat, "count": category_counts[cat]} for cat in all_categories
            ]

            return facets

        except Exception as e:
            logger.error(f"Error generating facets: {str(e)}")
            return {"categories": [], "primary_categories": [], "subcategories": {}}

    async def get_suggestions(self, partial_query: str, limit: int = 10) -> List[str]:
        """Get search suggestions based on partial query"""
        try:
            if not partial_query.strip():
                return []

            search_term = f"%{partial_query.strip()}%"

            # Get filename suggestions
            filename_suggestions = (
                self.db.query(Document.filename)
                .filter(
                    Document.filename.ilike(search_term),
                    Document.status == DocumentStatus.COMPLETED,
                )
                .limit(limit)
                .all()
            )

            suggestions = [filename[0] for filename in filename_suggestions]

            # Get keyword suggestions from documents
            docs_with_keywords = (
                self.db.query(Document)
                .filter(
                    Document.keywords.isnot(None),
                    Document.status == DocumentStatus.COMPLETED,
                )
                .limit(50)
                .all()
            )

            keyword_suggestions = set()
            for doc in docs_with_keywords:
                keywords = doc.get_keyword_list()
                for keyword in keywords:
                    if partial_query.lower() in keyword.lower():
                        keyword_suggestions.add(keyword)

            suggestions.extend(list(keyword_suggestions)[:limit])

            return list(set(suggestions))[:limit]

        except Exception as e:
            logger.error(f"Error getting suggestions: {str(e)}")
            return []

    async def get_recent_documents(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get recently uploaded documents"""
        try:
            documents = (
                self.db.query(Document)
                .filter(Document.status == DocumentStatus.COMPLETED)
                .order_by(desc(Document.created_at))
                .limit(limit)
                .all()
            )

            formatted_docs = []
            for doc in documents:
                formatted_doc = {
                    "id": doc.id,
                    "filename": doc.filename,
                    "created_at": (
                        doc.created_at.isoformat() if doc.created_at else None
                    ),
                    "summary": doc.get_summary(),
                    "categories": doc.get_categories(),
                }
                formatted_docs.append(formatted_doc)

            return formatted_docs

        except Exception as e:
            logger.error(f"Error getting recent documents: {str(e)}")
            return []

    async def get_popular_categories(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Get most popular document categories"""
        try:
            # Count categories across all documents
            category_counts = {}

            docs_with_keywords = (
                self.db.query(Document)
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords.isnot(None),
                )
                .all()
            )

            for doc in docs_with_keywords:
                categories = doc.get_categories()
                for category in categories:
                    category_counts[category] = category_counts.get(category, 0) + 1

            # Sort by count and return top categories
            sorted_categories = sorted(
                category_counts.items(), key=lambda x: x[1], reverse=True
            )[:limit]

            return [
                {"name": category, "count": count}
                for category, count in sorted_categories
            ]

        except Exception as e:
            logger.error(f"Error getting popular categories: {str(e)}")
            return []

    async def search_by_category(
        self, category: str, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search documents by specific category"""
        try:
            # Find documents with the specified category
            documents = []

            docs_with_keywords = (
                self.db.query(Document)
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords.isnot(None),
                )
                .all()
            )

            for doc in docs_with_keywords:
                if category in doc.get_categories():
                    documents.append(
                        {
                            "id": doc.id,
                            "filename": doc.filename,
                            "created_at": (
                                doc.created_at.isoformat() if doc.created_at else None
                            ),
                            "summary": doc.get_summary(),
                            "categories": doc.get_categories(),
                            "keywords": doc.get_keyword_list(),
                        }
                    )

            # Sort by creation date and limit
            documents.sort(key=lambda x: x["created_at"] or "", reverse=True)
            return documents[:limit]

        except Exception as e:
            logger.error(f"Error searching by category {category}: {str(e)}")
            return []

    async def get_search_stats(self) -> Dict[str, Any]:
        """Get search-related statistics"""
        try:
            total_searchable = (
                self.db.query(func.count(Document.id))
                .filter(Document.status == DocumentStatus.COMPLETED)
                .scalar()
            )

            total_with_keywords = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords.isnot(None),
                )
                .scalar()
            )

            total_categories = len(await self._get_all_categories())

            return {
                "total_searchable_documents": total_searchable,
                "documents_with_keywords": total_with_keywords,
                "total_categories": total_categories,
                "indexing_coverage": (
                    (total_with_keywords / total_searchable * 100)
                    if total_searchable > 0
                    else 0
                ),
            }

        except Exception as e:
            logger.error(f"Error getting search stats: {str(e)}")
            return {}

    async def _get_all_categories(self) -> List[str]:
        """Get all unique categories"""
        try:
            categories = set()

            docs_with_keywords = (
                self.db.query(Document)
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords.isnot(None),
                )
                .all()
            )

            for doc in docs_with_keywords:
                categories.update(doc.get_categories())

            return list(categories)

        except Exception as e:
            logger.error(f"Error getting all categories: {str(e)}")
            return []

    def __del__(self):
        """Cleanup database connection"""
        if hasattr(self, "db"):
            self.db.close()
