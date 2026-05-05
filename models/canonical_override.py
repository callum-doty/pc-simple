from sqlalchemy import Column, Integer, Text, DateTime, Index
from sqlalchemy.sql import func
from database import Base


class CanonicalOverride(Base):
    """
    Manual override table for client name canonicalization.

    Each row maps a normalized client name (client_clean_v1) to its canonical form.
    Takes precedence over all programmatic rules in the extraction pipeline.

    Replaces canonical_map_template.csv as the single source of truth.
    Enables real-time corrections, targeted reprocessing, and audit history.
    """

    __tablename__ = "canonical_overrides"

    id = Column(Integer, primary_key=True)
    client_clean_v1 = Column(Text, nullable=False, unique=True, index=True)
    client_canonical = Column(Text, nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), nullable=True)

    def __repr__(self):
        return f"<CanonicalOverride({self.client_clean_v1!r} → {self.client_canonical!r})>"
