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
