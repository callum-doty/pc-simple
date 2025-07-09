"""
Service layer for the simplified document catalog
"""

from services.document_service import DocumentService
from services.ai_service import AIService
from services.search_service import SearchService
from services.storage_service import StorageService

__all__ = ["DocumentService", "AIService", "SearchService", "StorageService"]
