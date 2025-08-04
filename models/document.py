"""
Simplified Document model - consolidates all document-related data into a single table
"""

from sqlalchemy import (
    Column,
    Integer,
    String,
    Text,
    DateTime,
    Float,
    Boolean,
    Index,
    Computed,
)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.sql import func
from datetime import datetime
from typing import Dict, Any, Optional, List
import json
from pgvector.sqlalchemy import Vector

from database import Base
from models.document_taxonomy_map import document_taxonomy_map
from models.taxonomy import TaxonomyTerm


class Document(Base):
    """
    Simplified document model that consolidates all document data
    Replaces the complex multi-table structure with a single table + JSON fields
    """

    __tablename__ = "documents"

    # Core fields
    id = Column(Integer, primary_key=True, index=True)
    filename = Column(String(255), nullable=False, index=True)
    file_path = Column(String(500), nullable=False)
    file_size = Column(Integer, nullable=False)

    # Status and processing
    status = Column(String(50), nullable=False, default="PENDING", index=True)
    processing_progress = Column(Integer, default=0)  # 0-100
    processing_error = Column(Text, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now(), index=True)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
    processed_at = Column(DateTime(timezone=True), nullable=True)

    # Content and analysis (JSON fields for flexibility)
    extracted_text = Column(Text, nullable=True)  # Raw extracted text
    ai_analysis = Column(JSONB, nullable=True)  # All AI analysis results
    keywords = Column(JSONB, nullable=True, index=True)  # Keywords and categories
    file_metadata = Column(JSONB, nullable=True)  # File metadata, page count, etc.

    # Search and embeddings
    search_content = Column(Text, nullable=True)
    search_vector = Column(Vector(3072), nullable=True)
    ts_vector = Column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(filename, '') || ' ' || coalesce(extracted_text, ''))",
            persisted=True,
        ),
    )

    # Preview and display
    preview_url = Column(String(500), nullable=True)
    thumbnail_url = Column(String(500), nullable=True)

    # Relationships
    taxonomy_terms = relationship(
        "TaxonomyTerm",
        secondary=document_taxonomy_map,
        back_populates="documents",
    )

    def __repr__(self):
        return f"<Document(id={self.id}, filename='{self.filename}', status='{self.status}')>"

    def get_summary(self) -> Optional[str]:
        """Get document summary from AI analysis"""
        if self.ai_analysis and isinstance(self.ai_analysis, dict):
            # Try to get summary from nested document_analysis structure first
            if "document_analysis" in self.ai_analysis:
                doc_analysis = self.ai_analysis["document_analysis"]
                if isinstance(doc_analysis, dict) and "summary" in doc_analysis:
                    return doc_analysis["summary"]

            # Fallback to direct summary field for backward compatibility
            return self.ai_analysis.get("summary")
        return None

    def get_categories(self) -> List[str]:
        """Get document categories from keywords"""
        if self.keywords and isinstance(self.keywords, dict):
            categories = self.keywords.get("categories", [])
            if isinstance(categories, list):
                return categories
        return []

    def get_keyword_list(self) -> List[str]:
        """Get list of keywords"""
        if self.keywords and isinstance(self.keywords, dict):
            keywords = self.keywords.get("keywords", [])
            if isinstance(keywords, list):
                return keywords
        return []

    def set_ai_analysis(self, analysis: Dict[str, Any]):
        """Set AI analysis results"""
        if not isinstance(analysis, dict):
            raise ValueError("Analysis must be a dictionary")
        self.ai_analysis = analysis

        # Update search content with analysis text
        search_parts = []
        if self.extracted_text:
            search_parts.append(self.extracted_text)
        if analysis.get("summary"):
            search_parts.append(analysis["summary"])
        if analysis.get("content_analysis"):
            search_parts.append(analysis["content_analysis"])

        # Add keywords and categories to search content
        if self.keywords:
            if self.keywords.get("keywords"):
                search_parts.extend(self.keywords["keywords"])
            if self.keywords.get("categories"):
                search_parts.extend(self.keywords["categories"])

        self.search_content = " ".join(search_parts)

    def set_keywords(self, keywords: List[str], categories: List[str] = None):
        """Set document keywords and categories"""
        self.keywords = {
            "keywords": keywords if isinstance(keywords, list) else [],
            "categories": categories if isinstance(categories, list) else [],
        }

        # Update search content
        search_parts = []
        if self.extracted_text:
            search_parts.append(self.extracted_text)
        if self.ai_analysis and self.ai_analysis.get("summary"):
            search_parts.append(self.ai_analysis.get("summary"))
        if self.ai_analysis and self.ai_analysis.get("content_analysis"):
            search_parts.append(self.ai_analysis.get("content_analysis"))

        if self.keywords:
            if self.keywords.get("keywords"):
                search_parts.extend(self.keywords["keywords"])
            if self.keywords.get("categories"):
                search_parts.extend(self.keywords["categories"])

        self.search_content = " ".join(search_parts)

    def set_metadata(self, **metadata):
        """Set document metadata"""
        if self.file_metadata is None:
            self.file_metadata = {}
        self.file_metadata.update(metadata)

    def get_metadata(self, key: str, default=None):
        """Get metadata value"""
        if self.file_metadata and isinstance(self.file_metadata, dict):
            return self.file_metadata.get(key, default)
        return default

    def update_processing_status(
        self, status: str, progress: int = None, error: str = None
    ):
        """Update processing status"""
        self.status = status
        if progress is not None:
            self.processing_progress = max(0, min(100, progress))
        if error:
            self.processing_error = error
        if status == "COMPLETED":
            self.processed_at = datetime.utcnow()
            self.processing_progress = 100

    def is_processing_complete(self) -> bool:
        """Check if document processing is complete"""
        return self.status == "COMPLETED"

    def is_processing_failed(self) -> bool:
        """Check if document processing failed"""
        return self.status == "FAILED"

    def can_be_reprocessed(self) -> bool:
        """Check if document can be reprocessed"""
        return self.status in ["FAILED", "PENDING", "PROCESSING"]

    def get_keyword_mappings(self) -> List[Dict[str, str]]:
        """Get rich keyword mappings from document"""
        if self.keywords and isinstance(self.keywords, dict):
            mappings = self.keywords.get("keyword_mappings", [])
            if isinstance(mappings, list):
                return mappings
        return []

    def get_mapping_count(self) -> int:
        """Get count of keyword mappings"""
        if self.keywords and isinstance(self.keywords, dict):
            return self.keywords.get("mapping_count", 0)
        return 0

    def get_verbatim_terms(self) -> List[str]:
        """Get list of verbatim terms extracted from document"""
        mappings = self.get_keyword_mappings()
        return [
            mapping.get("verbatim_term", "")
            for mapping in mappings
            if mapping.get("verbatim_term")
        ]

    def get_canonical_terms(self) -> List[str]:
        """Get list of canonical terms mapped from document"""
        mappings = self.get_keyword_mappings()
        return [
            mapping.get("mapped_canonical_term", "")
            for mapping in mappings
            if mapping.get("mapped_canonical_term")
        ]

    def to_dict(self, full_detail: bool = False) -> Dict[str, Any]:
        """
        Convert document to dictionary for API responses.
        - `full_detail=False`: Returns a summary view for search results.
        - `full_detail=True`: Returns the complete document object.
        """
        data = {
            "id": self.id,
            "filename": self.filename,
            "file_size": self.file_size,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "summary": self.get_summary(),
            "canonical_terms": self.get_canonical_terms(),
            "thumbnail_url": self.thumbnail_url,
            "preview_url": self.preview_url,
            "has_embeddings": self.search_vector is not None,
        }

        if full_detail:
            # Add heavyweight fields for detailed view
            data.update(
                {
                    "processing_progress": self.processing_progress,
                    "updated_at": (
                        self.updated_at.isoformat() if self.updated_at else None
                    ),
                    "processed_at": (
                        self.processed_at.isoformat() if self.processed_at else None
                    ),
                    "extracted_text": self.extracted_text,
                    "ai_analysis": self.ai_analysis,
                    "keywords": self.keywords,
                    "metadata": self.file_metadata,
                    "processing_error": self.processing_error,
                    "mapping_count": self.get_mapping_count(),
                    "verbatim_terms": self.get_verbatim_terms(),
                    "keyword_mappings": self.get_keyword_mappings(),
                }
            )
        return data


# Add indexes for FTS and vector search
Index("idx_documents_keywords", Document.keywords, postgresql_using="gin")
Index(
    "idx_documents_ts_vector",
    Document.ts_vector,
    postgresql_using="gin",
)
Index(
    "idx_documents_search_vector",
    Document.search_vector,
    postgresql_using="hnsw",
    postgresql_with={"m": 16, "ef_construction": 64},
)


# Status constants
class DocumentStatus:
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
