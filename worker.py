import os
from celery import Celery
from config import get_settings
from services.document_service import DocumentService
from services.ai_service import AIService
from services.storage_service import StorageService
from services.preview_service import PreviewService
from models.document import DocumentStatus
import logging
from typing import Generator, Tuple, List, Dict, Any
from collections import defaultdict

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


def _process_pdf_document_by_page(
    document_id: int,
    document,
    document_service: DocumentService,
    ai_service: AIService,
    storage_service: StorageService,
    analysis_type: str,
):
    """Helper function to process PDF documents page by page."""
    logger.info(f"Processing PDF document {document_id} page by page.")
    file_content = storage_service.get_file_sync(document.file_path)
    if not file_content:
        raise ValueError(f"Could not retrieve file content for {document.filename}")

    text_generator = ai_service.extract_text_from_pdf_sync_generator(file_content)

    aggregated_results: Dict[str, Any] = defaultdict(list)
    full_extracted_text = []
    page_summaries = []
    total_pages = 0  # We don't know total pages beforehand with a generator

    for page_num, page_text in text_generator:
        total_pages = page_num  # Keep track of the latest page number
        logger.info(f"Analyzing page {page_num} for document {document_id}")
        full_extracted_text.append(f"--- Page {page_num} ---\n{page_text}")

        # Analyze chunk
        chunk_analysis = ai_service.analyze_text_chunk_sync(
            page_text, document.filename, analysis_type
        )

        # Aggregate results
        keywords, categories = ai_service._extract_keywords_from_analysis(
            chunk_analysis
        )
        mappings = ai_service._extract_mappings_from_analysis(chunk_analysis)
        aggregated_results["keywords"].extend(keywords)
        aggregated_results["categories"].extend(categories)
        aggregated_results["mappings"].extend(mappings)

        # Try to find the summary in the nested structure
        if isinstance(chunk_analysis, dict):
            summary = chunk_analysis.get("summary")
            if not summary:
                doc_analysis = chunk_analysis.get("document_analysis", {})
                if isinstance(doc_analysis, dict):
                    summary = doc_analysis.get("summary")
            if summary:
                page_summaries.append(summary)

        # Update progress (estimate)
        # This is tricky with a generator. We can't know the total number of pages.
        # For now, we'll just show it's working. A better approach might be to
        # get page count first, but that adds an extra read operation.
        document_service.update_document_status_sync(
            document_id, DocumentStatus.PROCESSING, progress=50
        )

    # Consolidate results
    final_keywords = list(set(aggregated_results["keywords"]))
    final_categories = list(set(aggregated_results["categories"]))
    final_mappings = aggregated_results["mappings"]
    final_summary = "\n".join(page_summaries)
    final_extracted_text = "\n\n".join(full_extracted_text)

    final_ai_analysis = {
        "summary": final_summary,
        "page_count": total_pages,
        "analysis_type": "chunked_unified",
        "keyword_mappings": final_mappings,
    }

    # Update document with aggregated data
    document_service.update_document_content_sync(
        document_id,
        extracted_text=final_extracted_text,
        ai_analysis=final_ai_analysis,
        keywords=final_keywords,
        categories=final_categories,
        keyword_mappings=final_mappings,
        file_type="pdf",
    )

    # Generate embeddings from the consolidated summary
    if final_summary:
        logger.info(f"Generating embeddings from summary for document {document_id}")
        synthesized_text = " ".join(
            filter(None, [document.filename, final_extracted_text, final_summary])
        )
        embeddings = ai_service.generate_embeddings_sync(synthesized_text)
        if embeddings:
            document_service.update_document_embeddings_sync(document_id, embeddings)


def _process_document_holistically(
    document_id: int,
    document,
    document_service: DocumentService,
    ai_service: AIService,
    analysis_type: str,
):
    """Helper function for original, non-chunked processing."""
    logger.info(f"Processing document {document_id} holistically.")
    analysis_result = ai_service.analyze_document_sync(
        document.file_path, document.filename, analysis_type
    )

    if not analysis_result:
        raise ValueError("AI analysis failed")

    keywords, categories = ai_service._extract_keywords_from_analysis(
        analysis_result.get("ai_analysis", {})
    )
    mappings = ai_service._extract_mappings_from_analysis(
        analysis_result.get("ai_analysis", {})
    )

    document_service.update_document_content_sync(
        document_id,
        extracted_text=analysis_result.get("extracted_text"),
        ai_analysis=analysis_result.get("ai_analysis", {}),
        keywords=keywords,
        categories=categories,
        keyword_mappings=mappings,
        file_type=analysis_result.get("file_type"),
    )

    synthesized_text = " ".join(
        filter(None, [document.filename, analysis_result.get("extracted_text")])
    )
    if synthesized_text:
        embeddings = ai_service.generate_embeddings_sync(synthesized_text)
        if embeddings:
            document_service.update_document_embeddings_sync(document_id, embeddings)


from database import get_db


@celery_app.task(name="process_document_task")
def process_document_task(document_id: int, analysis_type: str = "unified"):
    """
    Celery task to process a single document.
    Delegates to chunked processing for PDFs and holistic for others.
    """
    db = next(get_db())
    document_service = DocumentService(db)
    ai_service = AIService(db)
    storage_service = StorageService()
    preview_service = PreviewService(storage_service)

    try:
        logger.info(
            f"Starting Celery processing for document {document_id} with {analysis_type} analysis"
        )

        document = document_service.get_document_sync(document_id)
        if not document:
            logger.error(f"Document {document_id} not found")
            return False

        document_service.update_document_status_sync(
            document_id, DocumentStatus.PROCESSING, progress=10
        )

        # Determine file type and processing strategy
        file_type = ai_service._get_file_type(document.filename)

        if file_type == "pdf":
            _process_pdf_document_by_page(
                document_id,
                document,
                document_service,
                ai_service,
                storage_service,
                analysis_type,
            )
        else:
            _process_document_holistically(
                document_id, document, document_service, ai_service, analysis_type
            )

        # Final steps for all types
        logger.info(f"Generating preview for document {document_id}")
        preview_path = preview_service.generate_preview_sync(document.file_path)
        if preview_path:
            preview_url = storage_service.get_file_url_sync(preview_path)
            if preview_url:
                document_service.update_document_preview_url_sync(
                    document_id, preview_url
                )

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
