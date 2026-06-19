"""
Shared dependencies and helpers for all API router modules.

Centralises items that were previously scattered as module-level globals in
main.py — Jinja2 template engine, common service factories, redirect helper.
Import from this module rather than from main.py to avoid circular imports.

See docs/architecture-fixes/FIX-006.
"""

from fastapi import Depends
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from database import get_db
from services.document_service import DocumentService
from services.ai_service import AIService
from services.search_service import SearchService
from services.storage_service import StorageService
from services.taxonomy_service import TaxonomyService
from services.preview_service import PreviewService

# ---------------------------------------------------------------------------
# Rate limiter — single shared instance used by all route modules.
# app.state.limiter is set to this object in main.py so the SlowAPIMiddleware
# enforces limits using the same backend as the decorators.
# See docs/architecture-fixes/FIX-006 (Bug B fix).
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, default_limits=["1000/hour"])


# ---------------------------------------------------------------------------
# Jinja2 templates — shared across all page-rendering routers
# ---------------------------------------------------------------------------

templates = Jinja2Templates(directory="templates")


# ---------------------------------------------------------------------------
# App-level mutable state written by main.py during startup.
# Route handlers read these to know whether Redis sessions are available.
# Using a simple namespace class avoids circular imports.
# ---------------------------------------------------------------------------

class _AppState:
    redis_session_middleware_installed: bool = False
    session_middleware_error: str | None = None


app_state = _AppState()


# ---------------------------------------------------------------------------
# Redirect helper (identical to create_redirect in main.py)
# ---------------------------------------------------------------------------

def create_redirect(url: str, status_code: int = 302) -> RedirectResponse:
    """Create a redirect response with cache-prevention headers."""
    response = RedirectResponse(url=url, status_code=status_code)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# ---------------------------------------------------------------------------
# Service dependency factories (mirror those in main.py — live here going forward)
# ---------------------------------------------------------------------------

def get_storage_service() -> StorageService:
    return StorageService()


def get_preview_service(
    storage_service: StorageService = Depends(get_storage_service),
) -> PreviewService:
    return PreviewService(storage_service)


def get_document_service(db: Session = Depends(get_db)) -> DocumentService:
    return DocumentService(db)


def get_ai_service(db: Session = Depends(get_db)) -> AIService:
    return AIService(db)


def get_search_service(
    db: Session = Depends(get_db),
    preview_service: PreviewService = Depends(get_preview_service),
    storage_service: StorageService = Depends(get_storage_service),
) -> SearchService:
    return SearchService(db, preview_service, storage_service)


def get_taxonomy_service(db: Session = Depends(get_db)) -> TaxonomyService:
    return TaxonomyService(db)
