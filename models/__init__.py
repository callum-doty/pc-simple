"""
Database models for the simplified document catalog
"""

from .document import Document
from .taxonomy import TaxonomyTerm, TaxonomySynonym

__all__ = ["Document", "TaxonomyTerm", "TaxonomySynonym"]
