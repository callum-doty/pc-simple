from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any, Optional

from database import get_db
from services.document_service import DocumentService

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
    Reset a document to QUEUED status and clear all AI-generated data for full reprocessing.
    This is useful for documents that failed during AI processing or have incomplete data.
    """
    doc_service = DocumentService(db)
    success = await doc_service.reset_document_for_reprocessing(document_id)

    if not success:
        raise HTTPException(
            status_code=404, detail="Document not found or could not be reset"
        )

    return {
        "success": True,
        "message": f"Document {document_id} has been reset and queued for reprocessing",
        "document_id": document_id,
    }
