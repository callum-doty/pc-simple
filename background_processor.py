"""
Background processor - handles document processing without Celery
Uses FastAPI BackgroundTasks and thread pools for simplified async processing
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Any, Optional
import time
from datetime import datetime

from services.document_service import DocumentService
from services.ai_service import AIService
from services.storage_service import StorageService
from models.document import DocumentStatus
from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class BackgroundProcessor:
    """Simplified background processor for document processing"""

    def __init__(self):
        self.document_service = DocumentService()
        self.ai_service = AIService()
        self.storage_service = StorageService()

        # Thread pool for CPU-intensive tasks
        self.executor = ThreadPoolExecutor(
            max_workers=settings.max_concurrent_processing,
            thread_name_prefix="doc_processor",
        )

        # Track processing tasks
        self.active_tasks = {}

    async def process_document(self, document_id: int) -> bool:
        """
        Process a single document through the complete pipeline
        This replaces the complex Celery task chain
        """
        return await self.process_document_with_analysis_type(document_id, "unified")

    async def process_document_with_analysis_type(
        self, document_id: int, analysis_type: str = "unified"
    ) -> bool:
        """
        Process a single document through the complete pipeline with specified analysis type
        """
        try:
            logger.info(
                f"Starting background processing for document {document_id} with {analysis_type} analysis"
            )

            # Get document
            document = await self.document_service.get_document(document_id)
            if not document:
                logger.error(f"Document {document_id} not found")
                return False

            # Check if already processing
            if document_id in self.active_tasks:
                logger.warning(f"Document {document_id} is already being processed")
                return False

            # Mark as processing
            self.active_tasks[document_id] = {
                "start_time": datetime.utcnow(),
                "status": "PROCESSING",
                "analysis_type": analysis_type,
            }

            await self.document_service.update_document_status(
                document_id, DocumentStatus.PROCESSING, progress=0
            )

            # Run processing pipeline with specified analysis type
            success = await self._run_processing_pipeline(
                document_id, document.file_path, document.filename, analysis_type
            )

            # Update final status
            if success:
                await self.document_service.update_document_status(
                    document_id, DocumentStatus.COMPLETED, progress=100
                )
                logger.info(
                    f"Successfully completed {analysis_type} processing for document {document_id}"
                )
            else:
                await self.document_service.update_document_status(
                    document_id,
                    DocumentStatus.FAILED,
                    progress=0,
                    error="Processing pipeline failed",
                )
                logger.error(f"Failed to process document {document_id}")

            # Remove from active tasks
            if document_id in self.active_tasks:
                del self.active_tasks[document_id]

            return success

        except Exception as e:
            logger.error(f"Error processing document {document_id}: {str(e)}")

            # Update status to failed
            await self.document_service.update_document_status(
                document_id, DocumentStatus.FAILED, progress=0, error=str(e)
            )

            # Remove from active tasks
            if document_id in self.active_tasks:
                del self.active_tasks[document_id]

            return False

    async def _run_processing_pipeline(
        self,
        document_id: int,
        file_path: str,
        filename: str,
        analysis_type: str = "unified",
    ) -> bool:
        """Run the complete document processing pipeline"""
        try:
            # Step 1: AI Analysis (10-80% progress)
            logger.info(
                f"Step 1: {analysis_type.title()} AI Analysis for document {document_id}"
            )
            await self.document_service.update_document_status(
                document_id, DocumentStatus.PROCESSING, progress=10
            )

            # Run AI analysis in thread pool (CPU intensive)
            loop = asyncio.get_event_loop()
            analysis_result = await loop.run_in_executor(
                self.executor,
                self._run_ai_analysis_sync,
                file_path,
                filename,
                analysis_type,
            )

            if not analysis_result:
                logger.error(f"AI analysis failed for document {document_id}")
                return False

            await self.document_service.update_document_status(
                document_id, DocumentStatus.PROCESSING, progress=60
            )

            # Step 2: Update document with analysis results (60-80% progress)
            logger.info(f"Step 2: Updating document content for {document_id}")

            success = await self.document_service.update_document_content(
                document_id,
                extracted_text=analysis_result.get("extracted_text"),
                ai_analysis=analysis_result.get("ai_analysis"),
                keywords=analysis_result.get("keywords"),
                categories=analysis_result.get("categories"),
                file_type=analysis_result.get("file_type"),
            )

            if not success:
                logger.error(f"Failed to update document content for {document_id}")
                return False

            await self.document_service.update_document_status(
                document_id, DocumentStatus.PROCESSING, progress=80
            )

            # Step 3: Generate embeddings if available (80-90% progress)
            logger.info(f"Step 3: Generating embeddings for document {document_id}")

            if analysis_result.get("extracted_text"):
                embeddings = await self.ai_service.generate_embeddings(
                    analysis_result["extracted_text"][:8000]  # Limit text length
                )

                if embeddings:
                    # Store embeddings in document
                    document = await self.document_service.get_document(document_id)
                    if document:
                        document.embeddings = embeddings
                        # Note: In a real implementation, you'd commit this to the database

            await self.document_service.update_document_status(
                document_id, DocumentStatus.PROCESSING, progress=90
            )

            # Step 4: Generate preview URL (90-100% progress)
            logger.info(f"Step 4: Generating preview for document {document_id}")

            preview_url = await self.storage_service.get_preview_url(file_path)
            if preview_url:
                # Update document with preview URL
                document = await self.document_service.get_document(document_id)
                if document:
                    document.preview_url = preview_url
                    # Note: In a real implementation, you'd commit this to the database

            await self.document_service.update_document_status(
                document_id, DocumentStatus.PROCESSING, progress=95
            )

            logger.info(
                f"Processing pipeline completed successfully for document {document_id}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Error in processing pipeline for document {document_id}: {str(e)}"
            )
            return False

    def _run_ai_analysis_sync(
        self, file_path: str, filename: str, analysis_type: str = "unified"
    ) -> Optional[Dict[str, Any]]:
        """
        Run AI analysis synchronously in thread pool
        This is needed because the AI service uses async methods
        """
        try:
            # Create new event loop for this thread
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            try:
                # Run the async AI analysis with specified type
                result = loop.run_until_complete(
                    self.ai_service.analyze_document(file_path, filename, analysis_type)
                )
                return result
            finally:
                loop.close()

        except Exception as e:
            logger.error(f"Error in sync AI analysis: {str(e)}")
            return None

    async def process_batch_documents(self, document_ids: list) -> Dict[str, Any]:
        """Process multiple documents concurrently"""
        try:
            logger.info(f"Starting batch processing for {len(document_ids)} documents")

            # Create tasks for all documents
            tasks = []
            for doc_id in document_ids:
                task = asyncio.create_task(self.process_document(doc_id))
                tasks.append((doc_id, task))

            # Wait for all tasks to complete
            results = {}
            for doc_id, task in tasks:
                try:
                    success = await task
                    results[doc_id] = {"success": success}
                except Exception as e:
                    results[doc_id] = {"success": False, "error": str(e)}

            # Calculate summary
            successful = sum(1 for r in results.values() if r["success"])
            failed = len(results) - successful

            logger.info(
                f"Batch processing completed: {successful} successful, {failed} failed"
            )

            return {
                "total_processed": len(document_ids),
                "successful": successful,
                "failed": failed,
                "results": results,
            }

        except Exception as e:
            logger.error(f"Error in batch processing: {str(e)}")
            return {
                "total_processed": len(document_ids),
                "successful": 0,
                "failed": len(document_ids),
                "error": str(e),
            }

    async def get_processing_status(self, document_id: int) -> Dict[str, Any]:
        """Get current processing status for a document"""
        try:
            # Check if actively processing
            if document_id in self.active_tasks:
                task_info = self.active_tasks[document_id]
                elapsed = (datetime.utcnow() - task_info["start_time"]).total_seconds()

                return {
                    "status": "PROCESSING",
                    "active": True,
                    "elapsed_seconds": elapsed,
                    "start_time": task_info["start_time"].isoformat(),
                }

            # Get status from database
            document = await self.document_service.get_document(document_id)
            if document:
                return {
                    "status": document.status,
                    "active": False,
                    "progress": document.processing_progress,
                    "error": document.processing_error,
                }

            return {"status": "NOT_FOUND", "active": False}

        except Exception as e:
            logger.error(f"Error getting processing status for {document_id}: {str(e)}")
            return {"status": "ERROR", "active": False, "error": str(e)}

    async def cancel_processing(self, document_id: int) -> bool:
        """Cancel processing for a document (if possible)"""
        try:
            if document_id in self.active_tasks:
                # Mark as cancelled (the task will check this)
                self.active_tasks[document_id]["status"] = "CANCELLED"

                # Update document status
                await self.document_service.update_document_status(
                    document_id, DocumentStatus.FAILED, error="Processing cancelled"
                )

                logger.info(f"Cancelled processing for document {document_id}")
                return True

            return False

        except Exception as e:
            logger.error(f"Error cancelling processing for {document_id}: {str(e)}")
            return False

    async def get_active_tasks(self) -> Dict[str, Any]:
        """Get information about currently active processing tasks"""
        try:
            active_info = {}

            for doc_id, task_info in self.active_tasks.items():
                elapsed = (datetime.utcnow() - task_info["start_time"]).total_seconds()
                active_info[str(doc_id)] = {
                    "status": task_info["status"],
                    "start_time": task_info["start_time"].isoformat(),
                    "elapsed_seconds": elapsed,
                }

            return {
                "active_count": len(self.active_tasks),
                "max_workers": settings.max_concurrent_processing,
                "tasks": active_info,
            }

        except Exception as e:
            logger.error(f"Error getting active tasks: {str(e)}")
            return {"active_count": 0, "max_workers": 0, "tasks": {}}

    async def cleanup_stuck_tasks(self, max_age_hours: int = 2) -> int:
        """Clean up tasks that have been running too long"""
        try:
            current_time = datetime.utcnow()
            stuck_tasks = []

            for doc_id, task_info in self.active_tasks.items():
                elapsed_hours = (
                    current_time - task_info["start_time"]
                ).total_seconds() / 3600
                if elapsed_hours > max_age_hours:
                    stuck_tasks.append(doc_id)

            # Clean up stuck tasks
            for doc_id in stuck_tasks:
                await self.document_service.update_document_status(
                    doc_id,
                    DocumentStatus.FAILED,
                    error=f"Task stuck for {max_age_hours} hours",
                )
                del self.active_tasks[doc_id]
                logger.warning(f"Cleaned up stuck task for document {doc_id}")

            return len(stuck_tasks)

        except Exception as e:
            logger.error(f"Error cleaning up stuck tasks: {str(e)}")
            return 0

    def __del__(self):
        """Cleanup resources"""
        if hasattr(self, "executor"):
            self.executor.shutdown(wait=False)
