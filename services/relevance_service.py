"""
Enhanced relevance scoring service for document search
Implements multi-factor scoring with dynamic weight adjustment
"""

import logging
import re
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy import func, case, and_, or_
from datetime import datetime, timedelta

from models.document import Document
from models.taxonomy import TaxonomyTerm
from models.search_query import SearchQuery
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class RelevanceService:
    """Advanced relevance scoring service with multi-factor analysis"""

    def __init__(self, db: Session):
        self.db = db

        # Scoring weights - can be tuned based on performance
        self.default_weights = {
            "vector": 0.4,
            "text": 0.25,
            "taxonomy": 0.15,
            "quality": 0.1,
            "freshness": 0.05,
            "popularity": 0.05,
        }

    def calculate_enhanced_relevance(
        self,
        query: str,
        base_query,
        search_subquery=None,
        canonical_term: Optional[str] = None,
        primary_category: Optional[str] = None,
    ):
        """
        Calculate enhanced relevance score incorporating multiple factors
        """
        try:
            # Analyze query characteristics
            query_analysis = self._analyze_query(query)

            # Get dynamic weights based on query type
            weights = self._get_dynamic_weights(
                query_analysis, canonical_term, primary_category
            )

            # Build relevance components
            relevance_components = []

            # 1. Base search relevance (vector + text)
            if search_subquery is not None:
                base_relevance = search_subquery.c.relevance
            else:
                base_relevance = func.coalesce(0.0, 0.0)

            relevance_components.append(
                (base_relevance * (weights["vector"] + weights["text"])).label(
                    "base_relevance"
                )
            )

            # 2. Taxonomy relevance
            taxonomy_relevance = self._calculate_taxonomy_relevance(
                query, query_analysis, canonical_term, primary_category
            )
            relevance_components.append(
                (taxonomy_relevance * weights["taxonomy"]).label("taxonomy_relevance")
            )

            # 3. Document quality score
            quality_score = self._calculate_quality_score()
            relevance_components.append(
                (quality_score * weights["quality"]).label("quality_relevance")
            )

            # 4. Freshness factor
            freshness_score = self._calculate_freshness_score()
            relevance_components.append(
                (freshness_score * weights["freshness"]).label("freshness_relevance")
            )

            # 5. Query popularity bonus
            popularity_score = self._calculate_popularity_score(query, query_analysis)
            relevance_components.append(
                (popularity_score * weights["popularity"]).label("popularity_relevance")
            )

            # Combine all relevance components
            final_relevance = sum(component for component in relevance_components)

            return final_relevance, weights, query_analysis

        except Exception as e:
            logger.error(f"Error calculating enhanced relevance: {str(e)}")
            # Fallback to base relevance if available
            if search_subquery is not None:
                return search_subquery.c.relevance, self.default_weights, {}
            else:
                return func.coalesce(0.0, 0.0), self.default_weights, {}

    def _analyze_query(self, query: str) -> Dict[str, Any]:
        """Analyze query characteristics to determine optimal scoring strategy"""
        if not query.strip():
            return {"type": "empty", "terms": [], "is_short": True, "has_quotes": False}

        # Clean and tokenize query
        clean_query = query.strip().lower()
        terms = re.findall(r"\b\w+\b", clean_query)

        analysis = {
            "original": query,
            "clean": clean_query,
            "terms": terms,
            "term_count": len(terms),
            "is_short": len(terms) <= 2,
            "is_long": len(terms) >= 5,
            "has_quotes": '"' in query,
            "has_boolean": any(word in clean_query for word in ["and", "or", "not"]),
            "potential_entity": self._detect_potential_entity(clean_query),
            "potential_category": self._detect_potential_category(clean_query),
        }

        # Classify query type
        if analysis["potential_entity"]:
            analysis["type"] = "entity"
        elif analysis["potential_category"]:
            analysis["type"] = "category"
        elif analysis["is_short"]:
            analysis["type"] = "short_keyword"
        elif analysis["has_quotes"]:
            analysis["type"] = "phrase"
        else:
            analysis["type"] = "general"

        return analysis

    def _detect_potential_entity(self, query: str) -> bool:
        """Detect if query might be searching for a specific entity (person, organization)"""
        # Simple heuristics - could be enhanced with NER
        entity_patterns = [
            r"\b[A-Z][a-z]+ [A-Z][a-z]+\b",  # FirstName LastName
            r"\b(campaign|committee|party|organization)\b",
            r"\b(candidate|opponent|client)\b",
        ]

        return any(
            re.search(pattern, query, re.IGNORECASE) for pattern in entity_patterns
        )

    def _detect_potential_category(self, query: str) -> bool:
        """Detect if query might be searching for a category or topic"""
        category_terms = [
            "healthcare",
            "education",
            "economy",
            "environment",
            "immigration",
            "defense",
            "taxes",
            "jobs",
            "infrastructure",
            "energy",
            "brochure",
            "flyer",
            "poster",
            "mailer",
            "advertisement",
        ]

        return any(term in query.lower() for term in category_terms)

    def _get_dynamic_weights(
        self,
        query_analysis: Dict[str, Any],
        canonical_term: Optional[str] = None,
        primary_category: Optional[str] = None,
    ) -> Dict[str, float]:
        """Get dynamic weights based on query characteristics"""
        weights = self.default_weights.copy()

        query_type = query_analysis.get("type", "general")

        if canonical_term or primary_category:
            # Boost taxonomy relevance for filtered searches
            weights["taxonomy"] = 0.25
            weights["vector"] = 0.35
            weights["text"] = 0.2
        elif query_type == "entity":
            # For entity queries, boost text matching and taxonomy
            weights["text"] = 0.35
            weights["taxonomy"] = 0.2
            weights["vector"] = 0.3
        elif query_type == "category":
            # For category queries, boost taxonomy heavily
            weights["taxonomy"] = 0.3
            weights["vector"] = 0.35
            weights["text"] = 0.2
        elif query_type == "short_keyword":
            # For short queries, boost vector similarity
            weights["vector"] = 0.5
            weights["text"] = 0.2
            weights["taxonomy"] = 0.15
        elif query_type == "phrase":
            # For phrase queries, boost text matching
            weights["text"] = 0.4
            weights["vector"] = 0.3
            weights["taxonomy"] = 0.15

        return weights

    def _calculate_taxonomy_relevance(
        self,
        query: str,
        query_analysis: Dict[str, Any],
        canonical_term: Optional[str] = None,
        primary_category: Optional[str] = None,
    ):
        """Calculate taxonomy-based relevance score"""
        try:
            # Base taxonomy score
            taxonomy_score = case(
                # Exact canonical term match gets highest score
                (
                    and_(
                        canonical_term.isnot(None) if canonical_term else False,
                        Document.taxonomy_terms.any(
                            TaxonomyTerm.term == canonical_term
                        ),
                    ),
                    1.0,
                ),
                # Primary category match gets good score
                (
                    and_(
                        primary_category.isnot(None) if primary_category else False,
                        Document.taxonomy_terms.any(
                            TaxonomyTerm.primary_category == primary_category
                        ),
                    ),
                    0.8,
                ),
                else_=0.0,
            )

            # Add keyword matching bonus for general queries
            if query.strip() and not canonical_term and not primary_category:
                # Check if query terms match any taxonomy terms
                query_terms = query_analysis.get("terms", [])
                if query_terms:
                    # Use JSONB keyword matching for additional relevance
                    keyword_match_bonus = case(
                        (
                            Document.keywords.op("@>")(
                                func.jsonb_build_array(
                                    *[
                                        func.jsonb_build_object("verbatim_term", term)
                                        for term in query_terms[
                                            :3
                                        ]  # Limit to first 3 terms
                                    ]
                                )
                            ),
                            0.3,
                        ),
                        else_=0.0,
                    )
                    taxonomy_score = taxonomy_score + keyword_match_bonus

            return taxonomy_score

        except Exception as e:
            logger.error(f"Error calculating taxonomy relevance: {str(e)}")
            return func.coalesce(0.0, 0.0)

    def _calculate_quality_score(self):
        """Calculate document quality score based on processing completeness"""
        try:
            quality_score = case(
                # Perfect processing
                (
                    and_(
                        Document.status == "COMPLETED",
                        Document.extracted_text.isnot(None),
                        Document.ai_analysis.isnot(None),
                        Document.search_vector.isnot(None),
                    ),
                    1.0,
                ),
                # Good processing (missing some components)
                (
                    and_(
                        Document.status == "COMPLETED",
                        Document.extracted_text.isnot(None),
                    ),
                    0.7,
                ),
                # Basic processing
                (Document.status == "COMPLETED", 0.5),
                # Incomplete processing
                else_=0.1,
            )

            # Boost documents with rich keyword mappings
            mapping_bonus = case(
                (
                    func.jsonb_array_length(
                        func.coalesce(
                            Document.keywords.op("->")("keyword_mappings"),
                            func.jsonb_build_array(),
                        )
                    )
                    > 5,
                    0.2,
                ),
                (
                    func.jsonb_array_length(
                        func.coalesce(
                            Document.keywords.op("->")("keyword_mappings"),
                            func.jsonb_build_array(),
                        )
                    )
                    > 2,
                    0.1,
                ),
                else_=0.0,
            )

            return quality_score + mapping_bonus

        except Exception as e:
            logger.error(f"Error calculating quality score: {str(e)}")
            return func.coalesce(1.0, 1.0)

    def _calculate_freshness_score(self):
        """Calculate freshness score based on document age"""
        try:
            # Documents newer than 30 days get a slight boost
            days_30_ago = datetime.utcnow() - timedelta(days=30)
            days_90_ago = datetime.utcnow() - timedelta(days=90)

            freshness_score = case(
                (Document.created_at >= days_30_ago, 1.0),
                (Document.created_at >= days_90_ago, 0.7),
                else_=0.5,
            )

            return freshness_score

        except Exception as e:
            logger.error(f"Error calculating freshness score: {str(e)}")
            return func.coalesce(1.0, 1.0)

    def _calculate_popularity_score(self, query: str, query_analysis: Dict[str, Any]):
        """Calculate popularity score based on search patterns"""
        try:
            if not query.strip():
                return func.coalesce(1.0, 1.0)

            # Get query terms for matching
            query_terms = query_analysis.get("terms", [])
            if not query_terms:
                return func.coalesce(1.0, 1.0)

            # Subquery to count how often documents match popular search terms
            popular_terms_subquery = (
                self.db.query(SearchQuery.query)
                .filter(
                    SearchQuery.created_at >= datetime.utcnow() - timedelta(days=30)
                )
                .subquery()
            )

            # For now, return a simple popularity score
            # In a full implementation, this would match document content against popular queries
            popularity_score = case(
                # Documents with embeddings (more processed) get slight boost
                (Document.search_vector.isnot(None), 1.2),
                else_=1.0,
            )

            return popularity_score

        except Exception as e:
            logger.error(f"Error calculating popularity score: {str(e)}")
            return func.coalesce(1.0, 1.0)

    def get_scoring_explanation(
        self,
        document_id: int,
        query: str,
        weights: Dict[str, float],
        query_analysis: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Get explanation of how a document's relevance score was calculated"""
        try:
            # This would be used for debugging and optimization
            return {
                "document_id": document_id,
                "query": query,
                "query_type": query_analysis.get("type", "unknown"),
                "weights_used": weights,
                "explanation": f"Query classified as '{query_analysis.get('type')}' type, "
                f"using dynamic weights with emphasis on "
                f"{max(weights, key=weights.get)} ({max(weights.values()):.1%})",
            }
        except Exception as e:
            logger.error(f"Error generating scoring explanation: {str(e)}")
            return {"error": str(e)}

    def tune_weights_based_on_feedback(
        self, query: str, clicked_documents: List[int], skipped_documents: List[int]
    ):
        """
        Tune scoring weights based on user feedback (future enhancement)
        This would implement a learning component to improve relevance over time
        """
        # Placeholder for machine learning-based weight optimization
        # Could use techniques like:
        # - Click-through rate analysis
        # - A/B testing of different weight combinations
        # - Reinforcement learning for weight adjustment
        pass
