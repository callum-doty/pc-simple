"""
Simplified Document model - consolidates all document-related data into a single table
"""

from sqlalchemy import Column, Integer, String, Text, DateTime, Float, JSON, Boolean
from sqlalchemy.sql import func
from datetime import datetime
from typing import Dict, Any, Optional, List
import json

from database import Base


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
    ai_analysis = Column(JSON, nullable=True)  # All AI analysis results
    keywords = Column(JSON, nullable=True)  # Keywords and categories
    file_metadata = Column(JSON, nullable=True)  # File metadata, page count, etc.

    # Search and embeddings
    search_content = Column(Text, nullable=True, index=True)  # Searchable text
    embeddings = Column(JSON, nullable=True)  # Vector embeddings as JSON

    # Preview and display
    preview_url = Column(String(500), nullable=True)
    thumbnail_url = Column(String(500), nullable=True)

    def __repr__(self):
        return f"<Document(id={self.id}, filename='{self.filename}', status='{self.status}')>"

    def to_dict(self) -> Dict[str, Any]:
        """Convert document to dictionary for API responses"""
        return {
            "id": self.id,
            "filename": self.filename,
            "file_size": self.file_size,
            "status": self.status,
            "processing_progress": self.processing_progress,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "processed_at": (
                self.processed_at.isoformat() if self.processed_at else None
            ),
            "extracted_text": self.extracted_text,
            "ai_analysis": self.ai_analysis,
            "keywords": self.keywords,
            "metadata": self.file_metadata,
            "preview_url": self.preview_url,
            "thumbnail_url": self.thumbnail_url,
            "processing_error": self.processing_error,
        }

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


# Status constants
class DocumentStatus:
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
