import os
from celery import Celery
from config import get_settings
from services.document_service import DocumentService
from services.ai_service import AIService
from services.storage_service import StorageService
from models.document import DocumentStatus
import logging

logger = logging.getLogger(__name__)

settings = get_settings()

celery_app = Celery(
    "worker",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
)


@celery_app.task(name="process_document_task")
def process_document_task(document_id: int, analysis_type: str = "unified"):
    """
    Celery task to process a single document.
    """
    document_service = DocumentService()
    ai_service = AIService()
    storage_service = StorageService()

    try:
        logger.info(
            f"Starting Celery processing for document {document_id} with {analysis_type} analysis"
        )

        # Get document
        document = document_service.get_document_sync(document_id)
        if not document:
            logger.error(f"Document {document_id} not found")
            return False

        document_service.update_document_status_sync(
            document_id, DocumentStatus.PROCESSING, progress=0
        )

        # Run processing pipeline
        # This is a simplified version of the original pipeline
        # In a real application, this would be more robust

        # Step 1: AI Analysis
        analysis_result = ai_service.analyze_document_sync(
            document.file_path, document.filename, analysis_type
        )

        if not analysis_result:
            logger.error(f"AI analysis failed for document {document_id}")
            document_service.update_document_status_sync(
                document_id,
                DocumentStatus.FAILED,
                progress=0,
                error="AI analysis failed",
            )
            return False

        # Step 2: Update document with extracted data
        keywords, categories = ai_service._extract_keywords_from_analysis(
            analysis_result.get("ai_analysis", {})
        )

        document_service.update_document_content_sync(
            document_id,
            extracted_text=analysis_result.get("extracted_text"),
            ai_analysis=analysis_result.get("ai_analysis", {}),
            keywords=keywords,
            categories=categories,
            file_type=analysis_result.get("file_type"),
        )

        # Step 3: Generate embeddings
        synthesized_text = " ".join(
            filter(None, [document.filename, analysis_result.get("extracted_text")])
        )
        embeddings = ai_service.generate_embeddings_sync(synthesized_text)
        if embeddings:
            document_service.update_document_embeddings_sync(document_id, embeddings)

        # Step 4: Generate preview URL
        preview_url = storage_service.get_preview_url_sync(document.file_path)
        if preview_url:
            document_service.update_document_preview_url_sync(document_id, preview_url)

        document_service.update_document_status_sync(
            document_id, DocumentStatus.COMPLETED, progress=100
        )
        logger.info(
            f"Successfully completed {analysis_type} processing for document {document_id}"
        )
        return True

    except Exception as e:
        logger.error(f"Error processing document {document_id}: {str(e)}")
        document_service.update_document_status_sync(
            document_id, DocumentStatus.FAILED, progress=0, error=str(e)
        )
        return False
