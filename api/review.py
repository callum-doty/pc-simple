"""
Review queue and facets API routes — moved from main.py as part of FIX-006.

Handles:
  GET  /api/facets/years
  GET  /api/facets/clients
  GET  /api/review/dates/count
  GET  /api/review/dates
  POST /api/review/dates/{document_id}
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, func as sa_func, extract
from sqlalchemy.orm import Session

from database import get_db
from models.document import Document

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/facets/years")
async def get_year_facets(db: Session = Depends(get_db)):
    """Return distinct years present in date_created, sorted ascending."""
    try:
        rows = (
            db.query(extract("year", Document.date_created).label("yr"))
            .filter(Document.date_created.isnot(None))
            .filter(Document.needs_date_review.isnot(True))
            .group_by("yr")
            .order_by("yr")
            .all()
        )
        years = [int(r[0]) for r in rows if 2019 <= int(r[0]) <= 2026]
        return {"success": True, "years": years}
    except Exception as e:
        logger.error(f"Year facets error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/facets/clients")
async def get_client_facets(db: Session = Depends(get_db)):
    """Return top 200 client_canonical values by document count."""
    try:
        rows = (
            db.query(Document.client_canonical, sa_func.count(Document.id).label("cnt"))
            .filter(Document.client_canonical.isnot(None))
            .filter(Document.client_canonical != "")
            .group_by(Document.client_canonical)
            .order_by(sa_func.count(Document.id).desc())
            .limit(200)
            .all()
        )
        return {"success": True, "clients": [r[0] for r in rows]}
    except Exception as e:
        logger.error(f"Client facets error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/review/dates/count")
async def date_review_count(db: Session = Depends(get_db)):
    """Return the number of documents pending any field review (for nav badge)."""
    try:
        count = (
            db.query(Document)
            .filter(or_(Document.needs_date_review == True, Document.needs_review == True))
            .count()
        )
        return {"success": True, "count": count}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/review/search")
async def search_documents(
    q: str = "",
    doc_id: int = None,
    db: Session = Depends(get_db),
):
    """Search any document by filename, client name, or ID for manual editing."""
    try:
        if not q and not doc_id:
            return {"success": True, "count": 0, "documents": []}

        query = db.query(Document)
        if doc_id:
            query = query.filter(Document.id == doc_id)
        else:
            like = f"%{q}%"
            query = query.filter(
                or_(
                    Document.filename.ilike(like),
                    Document.client_canonical.ilike(like),
                )
            )

        docs = query.order_by(Document.created_at.desc()).limit(25).all()
        return {
            "success": True,
            "count": len(docs),
            "documents": [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "needs_date_review": d.needs_date_review or False,
                    "needs_client_state_review": d.needs_review or False,
                    "date_created": d.date_created.isoformat() if d.date_created else None,
                    "date_confidence": d.date_confidence,
                    "client_canonical": d.client_canonical,
                    "client_confidence": d.client_confidence,
                    "state": d.state.strip() if d.state else None,
                    "state_confidence": d.state_confidence,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                }
                for d in docs
            ],
        }
    except Exception as e:
        logger.error(f"Document search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/review/dates")
async def list_date_review(db: Session = Depends(get_db)):
    """Return documents flagged for manual review of date, client, or state."""
    try:
        docs = (
            db.query(Document)
            .filter(or_(Document.needs_date_review == True, Document.needs_review == True))
            .order_by(Document.created_at.desc())
            .limit(500)
            .all()
        )
        return {
            "success": True,
            "count": len(docs),
            "documents": [
                {
                    "id": d.id,
                    "filename": d.filename,
                    "needs_date_review": d.needs_date_review or False,
                    "needs_client_state_review": d.needs_review or False,
                    "date_created": d.date_created.isoformat() if d.date_created else None,
                    "date_confidence": d.date_confidence,
                    "client_canonical": d.client_canonical,
                    "client_confidence": d.client_confidence,
                    "state": d.state.strip() if d.state else None,
                    "state_confidence": d.state_confidence,
                    "created_at": d.created_at.isoformat() if d.created_at else None,
                }
                for d in docs
            ],
        }
    except Exception as e:
        logger.error(f"Review list error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/review/dates/{document_id}")
async def resolve_date_review(
    document_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Save reviewer-corrected date, client, and/or state; clear review flags."""
    try:
        body = await request.json()
        doc = db.query(Document).filter(Document.id == document_id).first()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")

        # Date
        raw_date = body.get("date_created")
        if raw_date:
            try:
                parsed = datetime.strptime(raw_date, "%Y-%m-%d").date()
                if not (2019 <= parsed.year <= 2026):
                    raise HTTPException(
                        status_code=422,
                        detail="Date must be between 2019 and 2026",
                    )
                doc.date_created = parsed
            except ValueError:
                raise HTTPException(
                    status_code=422, detail="Invalid date format, use YYYY-MM-DD"
                )
        else:
            doc.date_created = None

        # Client
        client_val = body.get("client_canonical")
        if client_val is not None:
            doc.client_canonical = client_val.strip() or None
            doc.client_confidence = "HIGH"

        # State — two-letter code or empty
        state_val = body.get("state")
        if state_val is not None:
            cleaned = state_val.strip().upper()
            if cleaned and len(cleaned) != 2:
                raise HTTPException(
                    status_code=422, detail="State must be a 2-letter code"
                )
            doc.state = cleaned or None
            doc.state_confidence = "HIGH"

        doc.needs_date_review = False
        doc.needs_review = False
        db.commit()
        return {"success": True, "id": document_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Review resolve error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
