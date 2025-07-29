"""
Database model for the many-to-many relationship between documents and taxonomy terms.
"""

from sqlalchemy import Table, Column, Integer, ForeignKey
from database import Base

document_taxonomy_map = Table(
    "document_taxonomy_map",
    Base.metadata,
    Column("document_id", Integer, ForeignKey("documents.id"), primary_key=True),
    Column(
        "taxonomy_term_id", Integer, ForeignKey("taxonomy_terms.id"), primary_key=True
    ),
)
