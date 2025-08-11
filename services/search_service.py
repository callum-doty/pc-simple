"""
Search service - handles document search and filtering
Simplified search implementation with text-based and category filtering
"""

import logging
import re
from typing import Dict, Any, List, Optional
from sqlalchemy import or_, and_, func, desc, asc, cast, true, text
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
from sqlalchemy.orm import Session

from database import SessionLocal
from models.document import Document, DocumentStatus
from models.taxonomy import TaxonomyTerm
from models.search_query import SearchQuery
from config import get_settings
from services.preview_service import PreviewService
from services.ai_service import AIService
import datetime
import redis
import json

logger = logging.getLogger(__name__)
settings = get_settings()


class SearchService:
    """Service for searching and filtering documents"""

    def __init__(self, db: Session, preview_service: PreviewService):
        self.db = db
        self.preview_service = preview_service
        self.ai_service = AIService(db=self.db)
        try:
            if settings.redis_url:
                self.redis_client = redis.from_url(
                    settings.redis_url, decode_responses=True
                )
                self.redis_client.ping()
                logger.info("Redis cache connected successfully.")
            else:
                self.redis_client = None
        except redis.exceptions.ConnectionError as e:
            logger.error(f"Could not connect to Redis: {e}")
            self.redis_client = None

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
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy import cast

        try:
            # Build a query that filters by verbatim term in the JSONB array
            # This is a more robust way to query JSONB
            pattern = f"^{re.escape(verbatim_term)}$"
            path_expr = (
                '$.keyword_mappings[*] ? (@.verbatim_term like_regex $term flag "i")'
            )
            base_query = (
                self.db.query(Document)
                .filter(
                    text(
                        "jsonb_path_exists(documents.keywords, :path::jsonpath, :vars::jsonb)"
                    )
                )
                .params(path=path_expr, vars=json.dumps({"term": pattern}))
            )

            # Further refine with text search if a query is provided
            if query.strip():
                ts_query = func.plainto_tsquery("english", query)
                base_query = base_query.filter(Document.ts_vector.op("@@")(ts_query))

            # Order by creation date and limit results
            results = base_query.order_by(desc(Document.created_at)).limit(limit).all()

            # Format documents for response
            formatted_docs = [doc.to_dict(full_detail=False) for doc in results]
            return formatted_docs

        except Exception as e:
            logger.error(f"Error searching by verbatim term {verbatim_term}: {str(e)}")
            return []

    async def get_mapping_statistics(self) -> Dict[str, Any]:
        """Get statistics about keyword mappings across all documents"""
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy import cast

        try:
            # Count documents with keywords
            docs_with_keywords_count = (
                self.db.query(func.count(Document.id))
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    Document.keywords.isnot(None),
                )
                .scalar()
            )

            # Unnest keywords and perform aggregations
            keyword_element = func.jsonb_array_elements(
                func.coalesce(
                    Document.keywords.op("#>")("{keyword_mappings}"),
                    cast("[]", JSONB),
                )
            ).alias("keyword_element")

            # Total mappings
            total_mappings_query = self.db.query(func.count()).select_from(
                keyword_element
            )
            total_mappings = total_mappings_query.scalar()

            # Canonical term counts
            canonical_term_counts_query = (
                self.db.query(
                    keyword_element.c.value["mapped_canonical_term"].astext,
                    func.count(),
                )
                .filter(keyword_element.c.value["mapped_canonical_term"].isnot(None))
                .group_by(keyword_element.c.value["mapped_canonical_term"].astext)
                .order_by(func.count().desc())
                .limit(10)
            )
            top_canonical_terms = canonical_term_counts_query.all()

            # Primary category counts
            primary_category_counts_query = (
                self.db.query(
                    keyword_element.c.value["mapped_primary_category"].astext,
                    func.count(),
                )
                .filter(keyword_element.c.value["mapped_primary_category"].isnot(None))
                .group_by(keyword_element.c.value["mapped_primary_category"].astext)
            )
            primary_category_counts = dict(primary_category_counts_query.all())

            # Unique verbatim and canonical terms
            unique_verbatim_terms_query = self.db.query(
                func.count(
                    func.distinct(keyword_element.c.value["verbatim_term"].astext)
                )
            )
            unique_verbatim_terms = unique_verbatim_terms_query.scalar()

            unique_canonical_terms_query = self.db.query(
                func.count(
                    func.distinct(
                        keyword_element.c.value["mapped_canonical_term"].astext
                    )
                )
            )
            unique_canonical_terms = unique_canonical_terms_query.scalar()

            return {
                "total_documents_with_mappings": docs_with_keywords_count,
                "total_keyword_mappings": total_mappings,
                "average_mappings_per_document": (
                    total_mappings / docs_with_keywords_count
                    if docs_with_keywords_count
                    else 0
                ),
                "unique_verbatim_terms": unique_verbatim_terms,
                "unique_canonical_terms": unique_canonical_terms,
                "top_canonical_terms": [
                    {"term": term, "count": count}
                    for term, count in top_canonical_terms
                ],
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
        sort_by: str = "relevance",
        sort_direction: str = "desc",
    ) -> Dict[str, Any]:
        """
        Optimized search with hybrid text and vector search, hierarchical taxonomy filtering,
        and relevance scoring.
        """
        cache_key = f"search:{query}:{page}:{per_page}:{primary_category}:{subcategory}:{canonical_term}:{sort_by}:{sort_direction}"
        if self.redis_client:
            try:
                cached_result = self.redis_client.get(cache_key)
                if cached_result:
                    logger.info(f"Cache HIT for key: {cache_key}")
                    return json.loads(cached_result)
                logger.info(f"Cache MISS for key: {cache_key}")
            except redis.exceptions.RedisError as e:
                logger.error(f"Redis GET error: {e}")
        from sqlalchemy import literal_column, union_all, select
        from sqlalchemy.orm import undefer

        try:
            if query.strip():
                await self.log_search_query(query)

            search_subquery = None
            if query.strip():
                query_embedding = await self.ai_service.generate_embeddings(query)

                vector_weight = 0.7
                text_weight = 0.3

                vector_subquery = None
                if query_embedding is not None:
                    vector_subquery = (
                        select(
                            Document.id.label("id"),
                            (
                                1
                                - Document.search_vector.cosine_distance(
                                    query_embedding
                                )
                            ).label("vector_relevance"),
                        )
                        .filter(Document.search_vector.isnot(None))
                        .subquery()
                    )

                ts_query = func.plainto_tsquery("english", query)
                text_subquery = (
                    select(
                        Document.id.label("id"),
                        func.ts_rank_cd(Document.ts_vector, ts_query, 32).label(
                            "text_relevance"
                        ),
                    )
                    .filter(Document.ts_vector.op("@@")(ts_query))
                    .subquery()
                )

                # Build a combined subquery using FULL OUTER JOIN
                if vector_subquery is not None:
                    # If both vector and text search are available
                    combined_join = vector_subquery.join(
                        text_subquery,
                        vector_subquery.c.id == text_subquery.c.id,
                        isouter=True,
                    )

                    relevance_expression = (
                        func.coalesce(vector_subquery.c.vector_relevance, 0)
                        * vector_weight
                        + func.coalesce(text_subquery.c.text_relevance, 0) * text_weight
                    ).label("relevance")

                    id_expression = func.coalesce(
                        vector_subquery.c.id, text_subquery.c.id
                    )

                    search_subquery = (
                        select(id_expression.label("id"), relevance_expression)
                        .select_from(combined_join)
                        .subquery()
                    )
                else:
                    # Fallback to only text search
                    search_subquery = select(
                        text_subquery.c.id.label("id"),
                        (text_subquery.c.text_relevance * text_weight).label(
                            "relevance"
                        ),
                    ).subquery()

            # 2. Build the final query, joining with search results if applicable
            final_query = self.db.query(
                Document,
                (
                    search_subquery.c.relevance
                    if search_subquery is not None
                    else literal_column("0.0")
                ).label("relevance"),
            )

            if search_subquery is not None:
                final_query = final_query.join(
                    search_subquery, Document.id == search_subquery.c.id
                )
                final_query = final_query.filter(search_subquery.c.relevance > 0)

            # Apply filters
            final_query = final_query.filter(
                Document.status == DocumentStatus.COMPLETED
            )
            if primary_category:
                final_query = final_query.join(Document.taxonomy_terms).filter(
                    TaxonomyTerm.primary_category == primary_category
                )
            if subcategory:
                final_query = final_query.join(Document.taxonomy_terms).filter(
                    TaxonomyTerm.subcategory == subcategory
                )
            if canonical_term:
                logger.info(f"Applying canonical term filter for: {canonical_term}")
                # Use jsonb_path_exists for a direct and efficient query
                # Anchor the regex to prevent partial matches
                pattern = f"^{re.escape(canonical_term)}$"
                path_expr = '$.keyword_mappings[*] ? (@.mapped_canonical_term like_regex $term flag "i")'
                final_query = final_query.filter(
                    text(
                        "jsonb_path_exists(documents.keywords, :path::jsonpath, :vars::jsonb)"
                    )
                ).params(path=path_expr, vars=json.dumps({"term": pattern}))

            # 3. Get total count
            total_count = final_query.with_entities(func.count(Document.id)).scalar()

            # 4. Apply sorting
            if sort_by == "relevance" and search_subquery is not None:
                order_clause = desc("relevance")
            else:
                sort_column_name = "created_at" if sort_by == "relevance" else sort_by
                sort_column = getattr(Document, sort_column_name, Document.created_at)
                order_clause = (
                    desc(sort_column)
                    if sort_direction.lower() == "desc"
                    else asc(sort_column)
                )

            final_query = final_query.order_by(order_clause)

            # 5. Apply pagination
            offset = (page - 1) * per_page
            results = (
                final_query.options(
                    undefer(Document.ai_analysis), undefer(Document.search_vector)
                )
                .offset(offset)
                .limit(per_page)
                .all()
            )

            # 6. Format documents for response
            formatted_docs = []
            for doc, relevance in results:
                doc_dict = doc.to_dict(full_detail=False)
                doc_dict["relevance"] = f"{relevance:.2f}" if relevance else "0.00"
                formatted_docs.append(doc_dict)

            pagination = self._create_pagination_info(page, per_page, total_count)
            facets = await self._generate_enhanced_facets()

            result = {
                "documents": formatted_docs,
                "pagination": pagination,
                "facets": facets,
                "total_count": total_count,
                "query": query,
            }

            if self.redis_client:
                try:
                    self.redis_client.set(
                        cache_key, json.dumps(result, default=str), ex=3600
                    )  # Cache for 1 hour
                except redis.exceptions.RedisError as e:
                    logger.error(f"Redis SET error: {e}")

            return result

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
            # Facets for primary categories
            primary_category_facets = (
                self.db.query(TaxonomyTerm.primary_category, func.count(Document.id))
                .join(Document.taxonomy_terms)
                .group_by(TaxonomyTerm.primary_category)
                .order_by(desc(func.count(Document.id)))
                .all()
            )

            # Facets for subcategories
            subcategory_facets = (
                self.db.query(TaxonomyTerm.subcategory, func.count(Document.id))
                .join(Document.taxonomy_terms)
                .filter(TaxonomyTerm.subcategory.isnot(None))
                .group_by(TaxonomyTerm.subcategory)
                .order_by(desc(func.count(Document.id)))
                .all()
            )

            # Use jsonb_array_elements to unnest the keyword_mappings array
            keyword_element = func.jsonb_array_elements(
                func.coalesce(
                    Document.keywords.op("#>")("{keyword_mappings}"),
                    func.cast("[]", JSONB),
                )
            ).alias("keyword_element")

            # Then query the unnested elements
            canonical_term_facets = (
                self.db.query(
                    keyword_element.c.value["mapped_canonical_term"].astext,
                    func.count(Document.id),
                )
                .select_from(Document, keyword_element)
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    keyword_element.c.value["mapped_canonical_term"].isnot(None),
                )
                .group_by(keyword_element.c.value["mapped_canonical_term"].astext)
                .order_by(func.count(Document.id).desc())
                .limit(20)
                .all()
            )

            facets = {
                "primary_categories": [
                    {"name": cat, "count": count}
                    for cat, count in primary_category_facets
                ],
                "subcategories": [
                    {"name": sub, "count": count} for sub, count in subcategory_facets
                ],
                "canonical_terms": [
                    {"name": term, "count": count}
                    for term, count in canonical_term_facets
                ],
            }

            return facets

        except Exception as e:
            logger.error(f"Error generating enhanced facets: {str(e)}")
            return {
                "primary_categories": [],
                "subcategories": [],
                "canonical_terms": [],
            }

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
