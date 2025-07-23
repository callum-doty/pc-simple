"""
Document service - handles document CRUD operations and management
"""

from sqlalchemy.orm import Session
from sqlalchemy import func, desc, asc
from typing import List, Optional, Dict, Any
import logging
from datetime import datetime, timedelta

from database import SessionLocal
from models.document import Document, DocumentStatus
from config import get_settings
from worker import process_document_task

logger = logging.getLogger(__name__)
settings = get_settings()


class DocumentService:
    """Service for managing documents"""

    def __init__(self, db: Session):
        self.db = db

    async def create_document(
        self, filename: str, file_path: str, file_size: int, **metadata
    ) -> Document:
        """Create a new document record"""
        try:
            document = Document(
                filename=filename,
                file_path=file_path,
                file_size=file_size,
                status=DocumentStatus.PENDING,
            )

            # Set metadata if provided
            if metadata:
                document.set_metadata(**metadata)

            self.db.add(document)
            self.db.commit()
            self.db.refresh(document)

            # Trigger background processing
            if settings.environment != "testing":
                process_document_task.delay(document.id)

            logger.info(
                f"Created document: {filename} (ID: {document.id}) and triggered processing"
            )
            return document

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error creating document {filename}: {str(e)}")
            raise

    async def get_document(self, document_id: int) -> Optional[Document]:
        """Get document by ID"""
        try:
            return self.db.query(Document).filter(Document.id == document_id).first()
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
        keyword_mappings: List[Dict[str, str]] = None,  # Add this parameter
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
                document.set_ai_analysis(ai_analysis)

            # Enhanced keyword storage with mappings
            if keywords or categories or keyword_mappings:
                enhanced_keywords = {
                    "keywords": keywords or [],
                    "categories": categories or [],
                    "keyword_mappings": keyword_mappings
                    or [],  # Store the rich mappings
                    "mapping_count": len(keyword_mappings) if keyword_mappings else 0,
                    "extraction_timestamp": datetime.utcnow().isoformat(),
                }
                document.keywords = enhanced_keywords

            # Update metadata
            if metadata:
                document.set_metadata(**metadata)

            # Synthesize search_content
            search_parts = [document.filename]
            if ai_analysis:
                # Add summary if available
                if ai_analysis.get("summary"):
                    search_parts.append(ai_analysis.get("summary"))
                # Add content analysis if available
                if ai_analysis.get("content_analysis"):
                    search_parts.append(ai_analysis.get("content_analysis"))
                # Add title if available
                if ai_analysis.get("title"):
                    search_parts.append(ai_analysis.get("title"))

            # Add keywords and categories
            if keywords:
                search_parts.extend(keywords)
            if categories:
                search_parts.extend(categories)

            # Add verbatim terms from mappings
            if keyword_mappings:
                verbatim_terms = [
                    m["verbatim_term"] for m in keyword_mappings if "verbatim_term" in m
                ]
                search_parts.extend(verbatim_terms)

            # Add raw extracted text
            if extracted_text:
                search_parts.append(extracted_text)

            # Join all parts, ensuring they are strings and removing duplicates
            document.search_content = " ".join(
                sorted(list(set(str(p) for p in search_parts if p)))
            )

            self.db.commit()
            logger.info(
                f"Updated document {document_id} content with {len(keyword_mappings or [])} keyword mappings"
            )
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error updating document {document_id} content: {str(e)}")
            return False

    async def delete_document(self, document_id: int) -> bool:
        """Delete a document"""
        try:
            document = await self.get_document(document_id)
            if not document:
                return False

            self.db.delete(document)
            self.db.commit()

            logger.info(f"Deleted document {document_id}")
            return True

        except Exception as e:
            self.db.rollback()
            logger.error(f"Error deleting document {document_id}: {str(e)}")
            return False

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
                document.set_ai_analysis(ai_analysis)
            if keywords or categories or keyword_mappings:
                enhanced_keywords = {
                    "keywords": keywords or [],
                    "categories": categories or [],
                    "keyword_mappings": keyword_mappings or [],
                    "mapping_count": len(keyword_mappings) if keyword_mappings else 0,
                    "extraction_timestamp": datetime.utcnow().isoformat(),
                }
                document.keywords = enhanced_keywords
            if metadata:
                document.set_metadata(**metadata)

            search_parts = [document.filename]
            if ai_analysis:
                if ai_analysis.get("summary"):
                    search_parts.append(ai_analysis.get("summary"))
                if ai_analysis.get("content_analysis"):
                    search_parts.append(ai_analysis.get("content_analysis"))
                if ai_analysis.get("title"):
                    search_parts.append(ai_analysis.get("title"))
            if keywords:
                search_parts.extend(keywords)
            if categories:
                search_parts.extend(categories)
            if keyword_mappings:
                verbatim_terms = [
                    m["verbatim_term"] for m in keyword_mappings if "verbatim_term" in m
                ]
                search_parts.extend(verbatim_terms)
            if extracted_text:
                search_parts.append(extracted_text)

            document.search_content = " ".join(
                sorted(list(set(str(p) for p in search_parts if p)))
            )

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
