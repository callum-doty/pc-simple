"""
Document service - handles document CRUD operations and management
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, desc, asc
from typing import List, Optional, Dict, Any
import logging
from datetime import datetime, timedelta
from pathlib import Path
import redis

from database import SessionLocal
from models.document import Document, DocumentStatus
from models.taxonomy import TaxonomyTerm
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class DocumentService:
    """Service for managing documents"""

    def __init__(self, db: Session):
        self.db = db

    async def create_document(
        self, filename: str, file_path: str, file_size: int, **metadata
    ) -> Document:
        """Create a new document record and queue it for processing."""
        try:
            document = Document(
                filename=filename,
                file_path=file_path,
                file_size=file_size,
                status=DocumentStatus.QUEUED,  # Set status to QUEUED
            )

            # Set metadata if provided
            if metadata:
                document.set_metadata(**metadata)

            self.db.add(document)
            self.db.commit()
            self.db.refresh(document)

            logger.info(f"Created and queued document: {filename} (ID: {document.id})")
            return document

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error creating document {filename}: {str(e)}")
            raise

    async def get_document(self, document_id: int) -> Optional[Document]:
        """Get document by ID, ensuring heavyweight columns are loaded."""
        from sqlalchemy.orm import undefer

        try:
            # Explicitly undefer extracted_text to ensure it's loaded
            return (
                self.db.query(Document)
                .filter(Document.id == document_id)
                .options(undefer(Document.extracted_text))
                .first()
            )
        except Exception as e:
            logger.error(f"Error getting document {document_id}: {str(e)}")
            return None

    async def get_documents(
        self,
        skip: int = 0,
        limit: int = 100,
        status: Optional[str] = None,
        order_by: str = "created_at",
        order_direction: str = "desc",
    ) -> List[Document]:
        """Get list of documents with filtering and pagination"""
        try:
            query = self.db.query(Document)

            # Apply status filter
            if status:
                query = query.filter(Document.status == status)

            # Apply ordering
            if order_direction.lower() == "desc":
                query = query.order_by(desc(getattr(Document, order_by)))
            else:
                query = query.order_by(asc(getattr(Document, order_by)))

            # Apply pagination
            return query.offset(skip).limit(limit).all()

        except Exception as e:
            logger.error(f"Error getting documents: {str(e)}")
            return []

    async def update_document_status(
        self, document_id: int, status: str, progress: int = None, error: str = None
    ) -> bool:
        """Update document processing status"""
        try:
            document = await self.get_document(document_id)
            if not document:
                return False

            document.update_processing_status(status, progress, error)
            self.db.commit()

            logger.info(f"Updated document {document_id} status to {status}")
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error updating document {document_id} status: {str(e)}")
            return False

    async def update_document_content(
        self,
        document_id: int,
        extracted_text: str = None,
        ai_analysis: Dict[str, Any] = None,
        keywords: List[str] = None,
        categories: List[str] = None,
        keyword_mappings: List[Dict[str, str]] = None,
        **metadata,
    ) -> bool:
        """Update document content and analysis with rich keyword mappings"""
        try:
            document = await self.get_document(document_id)
            if not document:
                return False

            # Update extracted text
            if extracted_text:
                document.extracted_text = extracted_text

            # Update AI analysis
            if ai_analysis:
                document.ai_analysis = ai_analysis

            # Set keywords, categories, and mappings using the new model method
            if keywords or categories or keyword_mappings:
                document.set_keywords(
                    keywords=keywords,
                    categories=categories,
                    keyword_mappings=keyword_mappings,
                )

            # Update metadata
            if metadata:
                document.set_metadata(**metadata)

            # Update taxonomy mappings
            self._update_document_taxonomy_mappings(document, keyword_mappings)

            self.db.commit()
            logger.info(
                f"Updated document {document_id} content with {len(keyword_mappings or [])} keyword mappings"
            )
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error updating document {document_id} content: {str(e)}")
            return False

    async def delete_document(self, document_id: int, storage_service=None) -> Dict[str, Any]:
        """
        Delete a document completely - removes database record and all storage files.
        Returns detailed results of the deletion operation.
        """
        result = {
            "success": False,
            "document_id": document_id,
            "database_deleted": False,
            "file_deleted": False,
            "preview_deleted": False,
            "errors": []
        }
        
        try:
            document = await self.get_document(document_id)
            if not document:
                result["errors"].append("Document not found")
                return result

            filename = document.filename
            file_path = document.file_path
            
            # Delete files from storage if storage_service is provided
            if storage_service:
                # Delete main document file
                try:
                    if file_path:
                        file_deleted = await storage_service.delete_file(file_path)
                        result["file_deleted"] = file_deleted
                        if not file_deleted:
                            result["errors"].append(f"Failed to delete file: {file_path}")
                except Exception as e:
                    logger.error(f"Error deleting file {file_path}: {str(e)}")
                    result["errors"].append(f"File deletion error: {str(e)}")
                
                # Delete preview file
                try:
                    preview_path = f"previews/{Path(file_path).stem}_preview.png"
                    preview_deleted = await storage_service.delete_file(preview_path)
                    result["preview_deleted"] = preview_deleted
                    if not preview_deleted:
                        logger.info(f"Preview file not found or already deleted: {preview_path}")
                except Exception as e:
                    logger.error(f"Error deleting preview {preview_path}: {str(e)}")
                    result["errors"].append(f"Preview deletion error: {str(e)}")
            
            # Delete database record (CASCADE will handle taxonomy mappings)
            try:
                self.db.delete(document)
                self.db.commit()
                result["database_deleted"] = True
                result["success"] = True
                logger.info(f"Successfully deleted document {document_id} ({filename})")
                
                # Invalidate Redis cache after successful deletion
                self._invalidate_search_cache()
                
            except Exception as e:
                self.db.rollback()
                logger.error(f"Error deleting document {document_id} from database: {str(e)}")
                result["errors"].append(f"Database deletion error: {str(e)}")
                return result

            return result

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error deleting document {document_id}: {str(e)}")
            result["errors"].append(f"Unexpected error: {str(e)}")
            return result

    async def delete_documents_bulk(self, document_ids: List[int], storage_service=None) -> Dict[str, Any]:
        """
        Delete multiple documents in bulk.
        Returns detailed results for each document.
        """
        results = {
            "total_requested": len(document_ids),
            "successful": 0,
            "failed": 0,
            "details": []
        }
        
        for doc_id in document_ids:
            try:
                result = await self.delete_document(doc_id, storage_service)
                results["details"].append({
                    "document_id": doc_id,
                    "success": result["success"],
                    "database_deleted": result["database_deleted"],
                    "file_deleted": result["file_deleted"],
                    "preview_deleted": result["preview_deleted"],
                    "errors": result["errors"]
                })
                
                if result["success"]:
                    results["successful"] += 1
                else:
                    results["failed"] += 1
                    
            except Exception as e:
                logger.error(f"Error in bulk delete for document {doc_id}: {str(e)}")
                results["details"].append({
                    "document_id": doc_id,
                    "success": False,
                    "errors": [str(e)]
                })
                results["failed"] += 1
        
        logger.info(f"Bulk delete completed: {results['successful']}/{results['total_requested']} successful")
        
        # Invalidate Redis cache after bulk deletion if any documents were successfully deleted
        if results["successful"] > 0:
            self._invalidate_search_cache()
        
        return results

    async def get_statistics(self) -> Dict[str, Any]:
        """Get document statistics"""
        try:
            total_docs = self.db.query(func.count(Document.id)).scalar()

            # Count by status
            status_counts = {}
            for status in [
                DocumentStatus.PENDING,
                DocumentStatus.PROCESSING,
                DocumentStatus.COMPLETED,
                DocumentStatus.FAILED,
            ]:
                count = (
                    self.db.query(func.count(Document.id))
                    .filter(Document.status == status)
                    .scalar()
                )
                status_counts[status.lower()] = count

            # Recent activity (last 7 days)
            week_ago = datetime.utcnow() - timedelta(days=7)
            recent_uploads = (
                self.db.query(func.count(Document.id))
                .filter(Document.created_at >= week_ago)
                .scalar()
            )

            # Average file size
            avg_size = self.db.query(func.avg(Document.file_size)).scalar() or 0

            # Total storage used
            total_size = self.db.query(func.sum(Document.file_size)).scalar() or 0

            return {
                "total_documents": total_docs,
                "status_counts": status_counts,
                "recent_uploads": recent_uploads,
                "average_file_size": int(avg_size),
                "total_storage_bytes": int(total_size),
                "total_storage_mb": round(total_size / (1024 * 1024), 2),
            }

        except Exception as e:
            logger.error(f"Error getting statistics: {str(e)}")
            return {}

    async def get_failed_documents(self, limit: int = 50) -> List[Document]:
        """Get documents that failed processing"""
        try:
            return (
                self.db.query(Document)
                .filter(Document.status == DocumentStatus.FAILED)
                .order_by(desc(Document.updated_at))
                .limit(limit)
                .all()
            )

        except Exception as e:
            logger.error(f"Error getting failed documents: {str(e)}")
            return []

    async def get_stuck_documents(self, hours: int = 2) -> List[Document]:
        """Get documents stuck in processing"""
        try:
            cutoff_time = datetime.utcnow() - timedelta(hours=hours)

            return (
                self.db.query(Document)
                .filter(
                    Document.status == DocumentStatus.PROCESSING,
                    Document.updated_at < cutoff_time,
                )
                .order_by(desc(Document.updated_at))
                .all()
            )

        except Exception as e:
            logger.error(f"Error getting stuck documents: {str(e)}")
            return []

    async def search_documents_by_text(
        self, query: str, limit: int = 50
    ) -> List[Document]:
        """Simple text search in documents"""
        try:
            # Simple text search in filename and search_content
            search_term = f"%{query}%"

            return (
                self.db.query(Document)
                .filter(
                    (Document.filename.ilike(search_term))
                    | (Document.search_content.ilike(search_term))
                )
                .filter(Document.status == DocumentStatus.COMPLETED)
                .order_by(desc(Document.created_at))
                .limit(limit)
                .all()
            )

        except Exception as e:
            logger.error(f"Error searching documents: {str(e)}")
            return []

    def get_document_sync(self, document_id: int) -> Optional[Document]:
        """Get document by ID (synchronous)"""
        try:
            return self.db.query(Document).filter(Document.id == document_id).first()
        except Exception as e:
            logger.error(f"Error getting document {document_id}: {str(e)}")
            return None

    def update_document_status_sync(
        self, document_id: int, status: str, progress: int = None, error: str = None
    ) -> bool:
        """Update document processing status (synchronous)"""
        try:
            document = self.get_document_sync(document_id)
            if not document:
                return False

            document.update_processing_status(status, progress, error)
            self.db.commit()

            logger.info(f"Updated document {document_id} status to {status}")
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error updating document {document_id} status: {str(e)}")
            return False

    def update_document_content_sync(
        self,
        document_id: int,
        extracted_text: str = None,
        ai_analysis: Dict[str, Any] = None,
        keywords: List[str] = None,
        categories: List[str] = None,
        keyword_mappings: List[Dict[str, str]] = None,
        **metadata,
    ) -> bool:
        """Update document content and analysis with rich keyword mappings (synchronous)"""
        try:
            document = self.get_document_sync(document_id)
            if not document:
                return False

            if extracted_text:
                document.extracted_text = extracted_text
            if ai_analysis:
                document.ai_analysis = ai_analysis

            # Set keywords, categories, and mappings using the new model method
            if keywords or categories or keyword_mappings:
                document.set_keywords(
                    keywords=keywords,
                    categories=categories,
                    keyword_mappings=keyword_mappings,
                )

            if metadata:
                document.set_metadata(**metadata)

            # Update taxonomy mappings
            self._update_document_taxonomy_mappings(document, keyword_mappings)

            self.db.commit()
            logger.info(
                f"Updated document {document_id} content with {len(keyword_mappings or [])} keyword mappings"
            )
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error updating document {document_id} content: {str(e)}")
            return False

    async def update_document_embeddings(
        self, document_id: int, embeddings: List[float]
    ) -> bool:
        """Update document search vector (embeddings)"""
        try:
            document = await self.get_document(document_id)
            if not document:
                return False

            document.search_vector = embeddings
            self.db.commit()
            logger.info(f"Updated embeddings for document {document_id}")
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(
                f"Error updating embeddings for document {document_id}: {str(e)}"
            )
            return False

    def update_document_embeddings_sync(
        self, document_id: int, embeddings: List[float]
    ) -> bool:
        """Update document search vector (embeddings) (synchronous)"""
        try:
            document = self.get_document_sync(document_id)
            if not document:
                return False

            document.search_vector = embeddings
            self.db.commit()
            logger.info(f"Updated embeddings for document {document_id}")
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(
                f"Error updating embeddings for document {document_id}: {str(e)}"
            )
            return False

    async def update_document_preview_url(
        self, document_id: int, preview_url: str
    ) -> bool:
        """Update document preview URL"""
        try:
            document = await self.get_document(document_id)
            if not document:
                return False

            document.preview_url = preview_url
            self.db.commit()
            logger.info(f"Updated preview URL for document {document_id}")
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(
                f"Error updating preview URL for document {document_id}: {str(e)}"
            )
            return False

    async def get_document_details(self, document_id: int) -> Optional[Dict[str, Any]]:
        """Get full document details, including heavyweight fields."""
        try:
            document = await self.get_document(document_id)
            if not document:
                return None
            return document.to_dict(full_detail=True, include_heavy_fields=True)
        except Exception as e:
            logger.error(f"Error getting document details for {document_id}: {str(e)}")
            return None

    def _update_document_taxonomy_mappings(
        self, document: Document, keyword_mappings: List[Dict[str, str]]
    ):
        """Update the document's taxonomy term associations."""
        if keyword_mappings is None:
            return

        try:
            # Clear existing associations
            document.taxonomy_terms.clear()

            # Get all canonical terms from the mappings
            canonical_terms = {
                m["mapped_canonical_term"]
                for m in keyword_mappings
                if "mapped_canonical_term" in m
            }

            if not canonical_terms:
                return

            # Find all matching taxonomy terms in a single query
            terms_to_associate = (
                self.db.query(TaxonomyTerm)
                .filter(TaxonomyTerm.term.in_(canonical_terms))
                .all()
            )

            # Associate the found terms with the document
            for term in terms_to_associate:
                document.taxonomy_terms.append(term)

            logger.info(
                f"Updated {len(terms_to_associate)} taxonomy mappings for document {document.id}"
            )

        except Exception as e:
            logger.error(
                f"Error updating taxonomy mappings for document {document.id}: {str(e)}"
            )
            # Rollback is handled by the calling function
            raise

    def update_document_preview_url_sync(
        self, document_id: int, preview_url: str
    ) -> bool:
        """Update document preview URL (synchronous)"""
        try:
            document = self.get_document_sync(document_id)
            if not document:
                return False

            document.preview_url = preview_url
            self.db.commit()
            logger.info(f"Updated preview URL for document {document_id}")
            return True
        except Exception as e:
            self.db.rollback()
            logger.error(
                f"Error updating preview URL for document {document_id}: {str(e)}"
            )
            return False

    async def reset_document_for_reprocessing(self, document_id: int) -> bool:
        """Reset document to QUEUED status and clear all AI-generated data for full reprocessing"""
        try:
            document = await self.get_document(document_id)
            if not document:
                logger.error(f"Document {document_id} not found for reprocessing")
                return False

            # Clear all AI-generated data
            document.extracted_text = None
            document.ai_analysis = None
            document.keywords = None
            document.search_vector = None

            # Clear taxonomy associations
            document.taxonomy_terms.clear()

            # Reset status to QUEUED for reprocessing
            document.status = DocumentStatus.QUEUED
            document.processing_error = None
            document.progress = 0
            document.processed_at = None
            document.updated_at = datetime.utcnow()

            self.db.commit()
            logger.info(f"Reset document {document_id} for reprocessing")
            
            # Invalidate cache since document data changed
            self._invalidate_search_cache()
            
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(
                f"Error resetting document {document_id} for reprocessing: {str(e)}"
            )
            return False

    def _invalidate_search_cache(self):
        """
        Invalidate Redis search and facet caches.
        Called after document deletion, bulk deletion, or reprocessing.
        """
        try:
            if not settings.redis_url:
                logger.debug("No Redis URL configured, skipping cache invalidation")
                return
            
            redis_client = redis.from_url(settings.redis_url, decode_responses=True)
            
            # Get all search and facet cache keys
            search_keys = redis_client.keys("search:*")
            facet_keys = redis_client.keys("facets:*")
            
            all_keys = search_keys + facet_keys
            
            if all_keys:
                deleted = redis_client.delete(*all_keys)
                logger.info(f"Invalidated {deleted} cache keys ({len(search_keys)} search, {len(facet_keys)} facet)")
            else:
                logger.debug("No cache keys to invalidate")
                
        except redis.exceptions.ConnectionError as e:
            logger.warning(f"Could not connect to Redis for cache invalidation: {e}")
        except Exception as e:
            logger.error(f"Error invalidating cache: {e}")
