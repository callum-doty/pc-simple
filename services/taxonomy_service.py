"""
Taxonomy service for the simplified app
Manages structured categorization and filtering
"""

import logging
import csv
import os
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from database import SessionLocal
from models.taxonomy import TaxonomyTerm, TaxonomySynonym
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class TaxonomyService:
    """Service for managing taxonomy terms and hierarchical categorization"""

    def __init__(self, db: Session):
        self.db = db

    async def initialize_from_csv(self, csv_file_path: str) -> Tuple[bool, str]:
        """
        Initialize taxonomy from CSV file
        Expected format: primary_category,subcategory,term
        """
        try:
            if not os.path.exists(csv_file_path):
                logger.error(f"Taxonomy CSV file not found: {csv_file_path}")
                return False, f"File not found: {csv_file_path}"

            created_count = 0
            error_count = 0

            with open(csv_file_path, "r", encoding="utf-8") as file:
                reader = csv.DictReader(file)

                for row in reader:
                    try:
                        primary_category = row.get("primary_category", "").strip()
                        subcategory = row.get("subcategory", "").strip() or None
                        term = row.get("term", "").strip()

                        if not primary_category or not term:
                            logger.warning(f"Skipping row with missing data: {row}")
                            error_count += 1
                            continue

                        # Check if term already exists
                        existing = (
                            self.db.query(TaxonomyTerm)
                            .filter(
                                TaxonomyTerm.term == term,
                                TaxonomyTerm.primary_category == primary_category,
                                TaxonomyTerm.subcategory == (subcategory or None),
                            )
                            .first()
                        )

                        if existing:
                            logger.debug(f"Term already exists: {term}")
                            continue

                        # Create new taxonomy term
                        taxonomy_term = TaxonomyTerm(
                            term=term,
                            primary_category=primary_category,
                            subcategory=subcategory if subcategory else None,
                        )

                        self.db.add(taxonomy_term)
                        created_count += 1

                    except Exception as e:
                        logger.error(f"Error processing row {row}: {str(e)}")
                        error_count += 1
                        continue

                # Commit all changes
                self.db.commit()

            message = f"Successfully created {created_count} taxonomy terms"
            if error_count > 0:
                message += f" ({error_count} errors)"

            logger.info(message)
            return True, message

        except Exception as e:
            self.db.rollback()
            logger.error(f"Failed to initialize taxonomy: {str(e)}")
            return False, f"Initialization failed: {str(e)}"

    async def get_taxonomy_hierarchy(self) -> Dict[str, Any]:
        """
        Get the complete taxonomy hierarchy organized by primary category
        """
        try:
            terms = (
                self.db.query(TaxonomyTerm)
                .order_by(
                    TaxonomyTerm.primary_category,
                    TaxonomyTerm.subcategory,
                    TaxonomyTerm.term,
                )
                .all()
            )

            hierarchy = {}
            for term in terms:
                if term.primary_category not in hierarchy:
                    hierarchy[term.primary_category] = {}

                subcategory = term.subcategory or "General"
                if subcategory not in hierarchy[term.primary_category]:
                    hierarchy[term.primary_category][subcategory] = []

                hierarchy[term.primary_category][subcategory].append(
                    {
                        "id": term.id,
                        "term": term.term,
                        "description": term.description,
                    }
                )

            return hierarchy

        except Exception as e:
            logger.error(f"Error getting taxonomy hierarchy: {str(e)}")
            return {}

    async def get_filter_taxonomy_data(self) -> Dict[str, Any]:
        """
        Get the complete taxonomy hierarchy for filtering purposes.
        Returns a nested dictionary: {primary_category: {subcategory: [terms]}}
        """
        try:
            terms = (
                self.db.query(TaxonomyTerm)
                .order_by(
                    TaxonomyTerm.primary_category,
                    TaxonomyTerm.subcategory,
                    TaxonomyTerm.term,
                )
                .all()
            )

            hierarchy = {}
            for term in terms:
                if term.primary_category not in hierarchy:
                    hierarchy[term.primary_category] = {}

                subcategory = term.subcategory or "General"
                if subcategory not in hierarchy[term.primary_category]:
                    hierarchy[term.primary_category][subcategory] = []

                hierarchy[term.primary_category][subcategory].append(term.term)

            return hierarchy

        except Exception as e:
            logger.error(f"Error getting filter taxonomy data: {str(e)}")
            return {}

    async def get_primary_categories(self) -> List[Dict[str, Any]]:
        """Get all primary categories with counts"""
        try:
            categories = TaxonomyTerm.get_categories(self.db)

            category_data = []
            for category in categories:
                count = (
                    self.db.query(TaxonomyTerm)
                    .filter(TaxonomyTerm.primary_category == category)
                    .count()
                )
                category_data.append({"name": category, "count": count})

            return sorted(category_data, key=lambda x: x["name"])

        except Exception as e:
            logger.error(f"Error getting primary categories: {str(e)}")
            return []

    async def get_subcategories(self, primary_category: str) -> List[Dict[str, Any]]:
        """Get subcategories for a primary category"""
        try:
            subcategories = TaxonomyTerm.get_subcategories(self.db, primary_category)

            subcategory_data = []
            for subcategory in subcategories:
                count = (
                    self.db.query(TaxonomyTerm)
                    .filter(
                        TaxonomyTerm.primary_category == primary_category,
                        TaxonomyTerm.subcategory == subcategory,
                    )
                    .count()
                )
                subcategory_data.append({"name": subcategory, "count": count})

            return sorted(subcategory_data, key=lambda x: x["name"])

        except Exception as e:
            logger.error(f"Error getting subcategories: {str(e)}")
            return []

    async def search_terms(self, search_query: str) -> List[Dict[str, Any]]:
        """Search taxonomy terms"""
        try:
            terms = TaxonomyTerm.find_matching_terms(self.db, search_query)
            return [term.to_dict() for term in terms]

        except Exception as e:
            logger.error(f"Error searching terms: {str(e)}")
            return []

    async def get_term_hierarchy(self, term: str) -> Optional[Dict[str, str]]:
        """Get the full hierarchy for a given canonical term"""
        try:
            taxonomy_term = (
                self.db.query(TaxonomyTerm).filter(TaxonomyTerm.term == term).first()
            )
            if taxonomy_term:
                return {
                    "primary_category": taxonomy_term.primary_category,
                    "subcategory": taxonomy_term.subcategory,
                    "term": taxonomy_term.term,
                }
            return None
        except Exception as e:
            logger.error(f"Error getting term hierarchy for '{term}': {str(e)}")
            return None

    async def get_statistics(self) -> Dict[str, Any]:
        """Get taxonomy statistics"""
        try:
            total_terms = self.db.query(TaxonomyTerm).count()
            total_categories = len(TaxonomyTerm.get_categories(self.db))
            total_synonyms = self.db.query(TaxonomySynonym).count()

            # Get category breakdown
            category_counts = {}
            categories = TaxonomyTerm.get_categories(self.db)
            for category in categories:
                count = (
                    self.db.query(TaxonomyTerm)
                    .filter(TaxonomyTerm.primary_category == category)
                    .count()
                )
                category_counts[category] = count

            return {
                "total_terms": total_terms,
                "total_categories": total_categories,
                "total_synonyms": total_synonyms,
                "category_breakdown": category_counts,
            }

        except Exception as e:
            logger.error(f"Error getting taxonomy statistics: {str(e)}")
            return {}

    async def get_all_canonical_terms(self) -> Dict[str, List[str]]:
        """Get a dictionary of canonical terms grouped by primary category, with fallback to all taxonomy terms."""
        from sqlalchemy.dialects.postgresql import JSONB
        from sqlalchemy import func, cast
        from models.document import Document, DocumentStatus

        try:
            # First, try to get canonical terms that actually exist in document keyword mappings
            # Note: Cast json to jsonb since the keywords column is json type
            keyword_element = func.jsonb_array_elements(
                func.coalesce(
                    Document.keywords.op("::jsonb").op("#>")("{keyword_mappings}"),
                    cast("[]", JSONB),
                )
            ).alias("keyword_element")

            # Get distinct canonical terms from documents with their counts
            document_canonical_terms = (
                self.db.query(
                    keyword_element.c.value["mapped_canonical_term"].astext.label(
                        "canonical_term"
                    ),
                    func.count(Document.id).label("doc_count"),
                )
                .select_from(Document, keyword_element)
                .filter(
                    Document.status == DocumentStatus.COMPLETED,
                    keyword_element.c.value["mapped_canonical_term"].isnot(None),
                )
                .group_by(keyword_element.c.value["mapped_canonical_term"].astext)
                .having(
                    func.count(Document.id) > 0
                )  # Only terms that appear in at least one document
                .all()
            )

            # Get the taxonomy information for these terms
            canonical_terms_list = [term for term, count in document_canonical_terms]

            if canonical_terms_list:
                logger.info(
                    f"Found {len(canonical_terms_list)} canonical terms in document mappings"
                )

                # Get taxonomy information for these canonical terms
                taxonomy_terms = (
                    self.db.query(TaxonomyTerm.primary_category, TaxonomyTerm.term)
                    .filter(TaxonomyTerm.term.in_(canonical_terms_list))
                    .order_by(TaxonomyTerm.primary_category, TaxonomyTerm.term)
                    .all()
                )

                # Group by primary category
                grouped_terms = {}
                for primary_category, term in taxonomy_terms:
                    if primary_category not in grouped_terms:
                        grouped_terms[primary_category] = []
                    grouped_terms[primary_category].append(term)

                # Also include terms that exist in documents but not in taxonomy (with a fallback category)
                taxonomy_term_set = {term for _, term in taxonomy_terms}
                orphaned_terms = [
                    term
                    for term in canonical_terms_list
                    if term not in taxonomy_term_set
                ]

                if orphaned_terms:
                    logger.info(
                        f"Found {len(orphaned_terms)} canonical terms in documents that don't exist in taxonomy"
                    )
                    if "Other" not in grouped_terms:
                        grouped_terms["Other"] = []
                    grouped_terms["Other"].extend(sorted(orphaned_terms))

                logger.info(
                    f"Returning {sum(len(terms) for terms in grouped_terms.values())} canonical terms across {len(grouped_terms)} categories from document mappings"
                )
                return grouped_terms

            else:
                # Fallback: No canonical terms found in document mappings, return all taxonomy terms
                logger.warning(
                    "No canonical terms found in document mappings, falling back to all taxonomy terms"
                )

                taxonomy_terms = (
                    self.db.query(TaxonomyTerm.primary_category, TaxonomyTerm.term)
                    .order_by(TaxonomyTerm.primary_category, TaxonomyTerm.term)
                    .all()
                )

                grouped_terms = {}
                for primary_category, term in taxonomy_terms:
                    if primary_category not in grouped_terms:
                        grouped_terms[primary_category] = []
                    grouped_terms[primary_category].append(term)

                logger.info(
                    f"Fallback: Returning {sum(len(terms) for terms in grouped_terms.values())} canonical terms across {len(grouped_terms)} categories from taxonomy table"
                )
                return grouped_terms

        except Exception as e:
            logger.error(
                f"Error getting canonical terms from document mappings: {str(e)}"
            )

            # Final fallback: Return all taxonomy terms if there's an error
            try:
                logger.info(
                    "Attempting final fallback to all taxonomy terms due to error"
                )
                taxonomy_terms = (
                    self.db.query(TaxonomyTerm.primary_category, TaxonomyTerm.term)
                    .order_by(TaxonomyTerm.primary_category, TaxonomyTerm.term)
                    .all()
                )

                grouped_terms = {}
                for primary_category, term in taxonomy_terms:
                    if primary_category not in grouped_terms:
                        grouped_terms[primary_category] = []
                    grouped_terms[primary_category].append(term)

                logger.info(
                    f"Final fallback: Returning {sum(len(terms) for terms in grouped_terms.values())} canonical terms across {len(grouped_terms)} categories"
                )
                return grouped_terms

            except Exception as fallback_error:
                logger.error(f"Final fallback also failed: {str(fallback_error)}")
                return {}
