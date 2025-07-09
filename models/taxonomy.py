"""
Taxonomy models for the simplified app
Provides structured categorization of documents
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from datetime import datetime
from typing import Dict, Any, List, Optional

from database import Base


class TaxonomyTerm(Base):
    """
    Simplified taxonomy term model
    Represents a hierarchical categorization system
    """

    __tablename__ = "taxonomy_terms"

    id = Column(Integer, primary_key=True, index=True)
    term = Column(String(255), nullable=False, index=True)
    primary_category = Column(String(255), nullable=False, index=True)
    subcategory = Column(String(255), nullable=True, index=True)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Self-referential relationship for hierarchy
    parent_id = Column(Integer, ForeignKey("taxonomy_terms.id"), nullable=True)
    children = relationship("TaxonomyTerm", backref="parent", remote_side=[id])

    def __repr__(self):
        return f"<TaxonomyTerm(term='{self.term}', category='{self.primary_category}')>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "term": self.term,
            "primary_category": self.primary_category,
            "subcategory": self.subcategory,
            "description": self.description,
            "parent_id": self.parent_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def get_full_path(self) -> str:
        """Get the full hierarchical path of this term"""
        if self.subcategory:
            return f"{self.primary_category} > {self.subcategory} > {self.term}"
        else:
            return f"{self.primary_category} > {self.term}"

    @classmethod
    def find_matching_terms(cls, session, search_term: str) -> List["TaxonomyTerm"]:
        """Find taxonomy terms matching the search term"""
        return (
            session.query(cls)
            .filter(cls.term.ilike(f"%{search_term}%"))
            .order_by(cls.primary_category, cls.subcategory, cls.term)
            .all()
        )

    @classmethod
    def get_by_category(cls, session, primary_category: str) -> List["TaxonomyTerm"]:
        """Get all terms in a primary category"""
        return (
            session.query(cls)
            .filter(cls.primary_category == primary_category)
            .order_by(cls.subcategory, cls.term)
            .all()
        )

    @classmethod
    def get_categories(cls, session) -> List[str]:
        """Get all unique primary categories"""
        result = session.query(cls.primary_category).distinct().all()
        return [row[0] for row in result]

    @classmethod
    def get_subcategories(cls, session, primary_category: str) -> List[str]:
        """Get all subcategories for a primary category"""
        result = (
            session.query(cls.subcategory)
            .filter(
                cls.primary_category == primary_category,
                cls.subcategory.isnot(None),
            )
            .distinct()
            .all()
        )
        return [row[0] for row in result if row[0]]


class TaxonomySynonym(Base):
    """
    Synonyms for taxonomy terms to support variations in search terminology
    """

    __tablename__ = "taxonomy_synonyms"

    id = Column(Integer, primary_key=True, index=True)
    taxonomy_id = Column(Integer, ForeignKey("taxonomy_terms.id"), nullable=False)
    synonym = Column(String(255), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationship to taxonomy term
    taxonomy_term = relationship("TaxonomyTerm", backref="synonyms")

    def __repr__(self):
        return f"<TaxonomySynonym(synonym='{self.synonym}')>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for API responses"""
        return {
            "id": self.id,
            "taxonomy_id": self.taxonomy_id,
            "synonym": self.synonym,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
