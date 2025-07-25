"""
Search service - handles document search and filtering
Simplified search implementation with text-based and category filtering
"""

import logging
from typing import Dict, Any, List, Optional
from sqlalchemy import or_, and_, func, desc, asc
from sqlalchemy.orm import Session

from database import SessionLocal
from models.document import Document, DocumentStatus
from models.taxonomy import TaxonomyTerm
from models.search_query import SearchQuery
from config import get_settings
from services.preview_service import PreviewService
from services.ai_service import AIService
import datetime

logger = logging.getLogger(__name__)
settings = get_settings()


class SearchService:
    """Service for searching and filtering documents"""

    def __init__(self, db: Session, preview_service: PreviewService):
        self.db = db
        self.preview_service = preview_service
        self.ai_service = AIService(db=self.db)

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
        self, category: str, query: str = "", limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search documents by specific category with hybrid search"""
        try:
            # Use the main search function to get hybrid search results
            search_results = await self.search(
                query=query, per_page=limit * 2
            )  # Fetch more to filter
            all_documents = search_results.get("documents", [])

            # Filter results by category
            categorized_documents = []
            for doc in all_documents:
                if category in doc.get("categories", []):
                    categorized_documents.append(doc)

            return categorized_documents[:limit]

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

    async def search_by_canonical_term(
        self, canonical_term: str, query: str = "", limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search documents by canonical term with hybrid search"""
        try:
            # Use the main search function to get hybrid search results
            search_results = await self.search(
                query=query, canonical_term=canonical_term, per_page=limit
            )
            return search_results.get("documents", [])

        except Exception as e:
            logger.error(
                f"Error searching by canonical term {canonical_term}: {str(e)}"
            )
            return []

    async def search_by_verbatim_term(
        self, verbatim_term: str, query: str = "", limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Search documents by verbatim term with hybrid search"""
        try:
            # Use the main search function to get hybrid search results
            search_results = await self.search(
                query=query, per_page=limit * 2
            )  # Fetch more to filter
            all_documents = search_results.get("documents", [])

            # Filter results by verbatim term
            verbatim_documents = []
            for doc in all_documents:
                mappings = doc.get("keyword_mappings", [])
                for mapping in mappings:
                    if (
                        verbatim_term.lower()
                        in mapping.get("verbatim_term", "").lower()
                    ):
                        verbatim_documents.append(doc)
                        break  # Add once per document

            return verbatim_documents[:limit]

        except Exception as e:
            logger.error(f"Error searching by verbatim term {verbatim_term}: {str(e)}")
            return []

    async def get_mapping_statistics(self) -> Dict[str, Any]:
        """Get statistics about keyword mappings across all documents"""
        try:
            docs_with_keywords = (
                self.db.query(Document)
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords.isnot(None),
                )
                .all()
            )

            total_mappings = 0
            canonical_term_counts = {}
            primary_category_counts = {}
            verbatim_terms = set()

            for doc in docs_with_keywords:
                mappings = doc.get_keyword_mappings()
                total_mappings += len(mappings)

                for mapping in mappings:
                    # Count canonical terms
                    canonical_term = mapping.get("mapped_canonical_term", "")
                    if canonical_term:
                        canonical_term_counts[canonical_term] = (
                            canonical_term_counts.get(canonical_term, 0) + 1
                        )

                    # Count primary categories
                    primary_category = mapping.get("mapped_primary_category", "")
                    if primary_category:
                        primary_category_counts[primary_category] = (
                            primary_category_counts.get(primary_category, 0) + 1
                        )

                    # Collect unique verbatim terms
                    verbatim_term = mapping.get("verbatim_term", "")
                    if verbatim_term:
                        verbatim_terms.add(verbatim_term)

            return {
                "total_documents_with_mappings": len(docs_with_keywords),
                "total_keyword_mappings": total_mappings,
                "average_mappings_per_document": (
                    total_mappings / len(docs_with_keywords)
                    if docs_with_keywords
                    else 0
                ),
                "unique_verbatim_terms": len(verbatim_terms),
                "unique_canonical_terms": len(canonical_term_counts),
                "top_canonical_terms": sorted(
                    canonical_term_counts.items(), key=lambda x: x[1], reverse=True
                )[:10],
                "primary_category_distribution": primary_category_counts,
            }

        except Exception as e:
            logger.error(f"Error getting mapping statistics: {str(e)}")
            return {}

    async def search(
        self,
        query: str = "",
        page: int = 1,
        per_page: int = 20,
        primary_category: Optional[str] = None,
        subcategory: Optional[str] = None,
        canonical_term: Optional[str] = None,
        sort_by: str = "created_at",
        sort_direction: str = "desc",
    ) -> Dict[str, Any]:
        """
        Optimized search with hierarchical taxonomy filtering, pushing all operations to the database.
        """
        try:
            if query.strip():
                await self.log_search_query(query)

            base_query = self.db.query(Document).filter(
                Document.status == DocumentStatus.COMPLETED
            )

            # Apply text and vector search
            if query.strip():
                query_embedding = await self.ai_service.generate_embeddings(query)
                vector_subquery = None
                if query_embedding is not None:
                    vector_subquery = (
                        self.db.query(Document.id)
                        .filter(Document.search_vector.isnot(None))
                        .order_by(Document.search_vector.l2_distance(query_embedding))
                        .limit(100)
                        .subquery()
                    )

                keywords = self._get_search_keywords(query)
                text_search_clauses = []
                if keywords:
                    for keyword in keywords:
                        search_term = f"%{keyword}%"
                        text_search_clauses.append(
                            or_(
                                Document.filename.ilike(search_term),
                                Document.search_content.ilike(search_term),
                                Document.extracted_text.ilike(search_term),
                            )
                        )

                text_subquery = (
                    self.db.query(Document.id)
                    .filter(and_(*text_search_clauses))
                    .subquery()
                )

                # Combine text and vector search results
                if vector_subquery is not None:
                    base_query = base_query.filter(
                        or_(
                            Document.id.in_(self.db.query(vector_subquery.c.id)),
                            Document.id.in_(self.db.query(text_subquery.c.id)),
                        )
                    )
                else:
                    base_query = base_query.filter(
                        Document.id.in_(self.db.query(text_subquery.c.id))
                    )

            # Apply hierarchical taxonomy filters
            if primary_category:
                base_query = base_query.filter(
                    Document.keywords["keyword_mappings"].astext.ilike(
                        f'%"{primary_category}"%'
                    )
                )
            if subcategory:
                base_query = base_query.filter(
                    Document.keywords["keyword_mappings"].astext.ilike(
                        f'%"{subcategory}"%'
                    )
                )
            if canonical_term:
                base_query = base_query.filter(
                    Document.keywords["keyword_mappings"].astext.ilike(
                        f'%"{canonical_term}"%'
                    )
                )

            # Get total count before pagination
            total_count = base_query.with_entities(func.count(Document.id)).scalar()

            # Apply sorting
            sort_column = getattr(Document, sort_by, Document.created_at)
            if sort_direction.lower() == "desc":
                base_query = base_query.order_by(desc(sort_column))
            else:
                base_query = base_query.order_by(asc(sort_column))

            # Apply pagination
            offset = (page - 1) * per_page
            documents = base_query.offset(offset).limit(per_page).all()

            # Format documents for response
            formatted_docs = [doc.to_dict() for doc in documents]

            pagination = self._create_pagination_info(page, per_page, total_count)
            facets = await self._generate_enhanced_facets()

            return {
                "documents": formatted_docs,
                "pagination": pagination,
                "facets": facets,
                "total_count": total_count,
                "query": query,
            }

        except Exception as e:
            logger.error(f"Error in optimized search: {str(e)}")
            return {
                "documents": [],
                "pagination": {"page": 1, "per_page": per_page, "total": 0, "pages": 0},
                "facets": {"categories": [], "canonical_terms": []},
                "total_count": 0,
                "query": query,
                "error": str(e),
            }

    async def _generate_enhanced_facets(self) -> Dict[str, Any]:
        """Generate enhanced facets including canonical terms using efficient queries."""
        try:
            facets = await self._generate_facets()

            # Efficiently count canonical terms
            canonical_term_counts = (
                self.db.query(
                    func.json_array_elements_text(
                        Document.keywords["keyword_mappings"]
                    ).label("canonical_term"),
                    func.count().label("count"),
                )
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords.isnot(None),
                )
                .group_by("canonical_term")
                .order_by(desc("count"))
                .limit(20)
                .all()
            )

            facets["canonical_terms"] = [
                {"name": term, "count": count} for term, count in canonical_term_counts
            ]

            return facets

        except Exception as e:
            logger.error(f"Error generating enhanced facets: {str(e)}")
            return {"categories": [], "canonical_terms": []}

    def _get_search_keywords(self, query: str) -> List[str]:
        """Extracts keywords from a search query, filtering out stop words."""
        stop_words = set(
            [
                "a",
                "an",
                "and",
                "the",
                "in",
                "on",
                "of",
                "for",
                "with",
                "is",
                "are",
                "was",
                "were",
            ]
        )
        return [
            keyword
            for keyword in query.lower().split()
            if keyword.strip() and keyword not in stop_words
        ]

    async def log_search_query(self, query: str, user_id: Optional[str] = None):
        """Logs a search query to the database."""
        try:
            search_query = SearchQuery(query=query.strip(), user_id=user_id)
            self.db.add(search_query)
            self.db.commit()
        except Exception as e:
            logger.error(f"Error logging search query: {str(e)}")
            self.db.rollback()

    async def get_top_queries(self, limit: int = 8) -> List[Dict[str, Any]]:
        """Gets the most frequent search queries."""
        try:
            top_queries = (
                self.db.query(
                    SearchQuery.query, func.count(SearchQuery.query).label("count")
                )
                .group_by(SearchQuery.query)
                .order_by(desc("count"))
                .limit(limit)
                .all()
            )
            return [{"query": q, "count": c} for q, c in top_queries]
        except Exception as e:
            logger.error(f"Error getting top queries: {str(e)}")
            return []
