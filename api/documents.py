from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional, List
import logging

from database import get_db
from services.document_service import DocumentService
from services.storage_service import StorageService
from config import get_settings
from worker import process_document_task

logger = logging.getLogger(__name__)
settings = get_settings()
router = APIRouter()


@router.get("/documents/{document_id}/details", response_model=Optional[Dict[str, Any]])
async def get_document_details(document_id: int, db: Session = Depends(get_db)):
    """
    Get full details for a single document, including heavyweight fields
    like extracted_text and ai_analysis.
    """
    doc_service = DocumentService(db)
    details = await doc_service.get_document_details(document_id)
    if not details:
        raise HTTPException(status_code=404, detail="Document not found")
    return details


@router.post("/documents/{document_id}/reprocess")
async def reprocess_document(document_id: int, db: Session = Depends(get_db)):
    """
    Reset a document and immediately dispatch for reprocessing via Celery worker.
    Clears all AI-generated data and triggers immediate reprocessing.
    """
    doc_service = DocumentService(db)

    # First, reset the document and clear all AI data
    success = await doc_service.reset_document_for_reprocessing(document_id)

    if not success:
        raise HTTPException(
            status_code=404, detail="Document not found or could not be reset"
        )

    # Immediately dispatch the Celery task for processing
    task = process_document_task.delay(document_id)

    return {
        "success": True,
        "message": f"Document {document_id} has been dispatched for reprocessing",
        "document_id": document_id,
        "task_id": task.id,
    }


@router.delete("/documents/{document_id}")
async def delete_document(
    document_id: int,
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    Delete a document completely - removes database record and all storage files.
    Requires upload password authentication.
    """
    try:
        # Verify password
        upload_password = settings.upload_password or "upload123"
        if password != upload_password:
            raise HTTPException(status_code=401, detail="Invalid password")
        
        # Initialize services
        doc_service = DocumentService(db)
        storage_service = StorageService()
        
        # Delete document
        result = await doc_service.delete_document(document_id, storage_service)
        
        if not result["success"]:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": "Failed to delete document",
                    "errors": result["errors"]
                }
            )
        
        return {
            "success": True,
            "message": f"Document {document_id} deleted successfully",
            "result": result
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting document {document_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/documents/bulk-delete")
async def bulk_delete_documents(
    document_ids: List[int] = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db)
):
    """
    Delete multiple documents in bulk.
    Requires upload password authentication.
    """
    try:
        # Verify password
        upload_password = settings.upload_password or "upload123"
        if password != upload_password:
            raise HTTPException(status_code=401, detail="Invalid password")
        
        # Initialize services
        doc_service = DocumentService(db)
        storage_service = StorageService()
        
        # Delete documents
        results = await doc_service.delete_documents_bulk(document_ids, storage_service)
        
        return {
            "success": True,
            "message": f"Deleted {results['successful']} of {results['total_requested']} documents",
            "results": results
        }
    
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in bulk delete: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
