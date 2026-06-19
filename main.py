"""
Simplified Document Catalog Application
FastAPI-based rebuild with streamlined architecture
"""

from fastapi import (
    FastAPI,
    BackgroundTasks,
    HTTPException,
    Depends,
    UploadFile,
    File,
    Form,
    Request,
    Header,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

# from starlette.middleware.sessions import SessionMiddleware  # Replaced with Redis-based solution
from contextlib import asynccontextmanager
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import os
from typing import List, Optional
import logging
from sqlalchemy.orm import Session
from pathlib import Path
from datetime import datetime, timedelta

from config import get_settings, validate_storage_config
from database import init_db, get_db
from services.document_service import DocumentService
from services.ai_service import AIService
from services.search_service import SearchService
from services.storage_service import StorageService
from services.taxonomy_service import TaxonomyService
from services.preview_service import PreviewService
from services.security_service import security_service
from api.dashboard import router as dashboard_router
from api.documents import router as documents_router
from api.search import router as search_router
from api.taxonomy import router as taxonomy_router
from api.review import router as review_router
from api.admin import router as admin_router
from api.dependencies import app_state, limiter
from worker import process_document_task
from celery.result import AsyncResult
from models.search_query import SearchQuery
from models.document import Document

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Set sqlalchemy engine logger to WARNING to reduce verbosity
logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

# Performance monitoring
import time
from contextlib import asynccontextmanager as async_context

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager"""
    # Startup
    logger.info("Starting up Document Catalog application...")

    # Schema migrations are applied by the build process (render.yaml buildCommand)
    # before any instance starts. Running them here as well creates a race condition
    # if two instances start simultaneously. See docs/architecture-fixes/FIX-003.
    logger.info("Schema migrations are applied during the build phase before startup.")

    await init_db()

    # Validate storage configuration — raises RuntimeError in production if S3
    # credentials are missing when STORAGE_TYPE=s3. See docs/architecture-fixes/FIX-005.
    try:
        validate_storage_config(settings)
        logger.info(f"Storage configuration validated: type={settings.storage_type}")
    except RuntimeError as storage_err:
        logger.error(f"Storage configuration error: {storage_err}")
        raise

    # Initialize taxonomy from CSV if it exists
    taxonomy_csv_path = "taxonomy.csv"
    if os.path.exists(taxonomy_csv_path):
        logger.info("Initializing taxonomy from CSV...")
        # Create a temporary service instance for initialization
        db_session = next(get_db())
        taxonomy_service = TaxonomyService(db_session)
        success, message = await taxonomy_service.initialize_from_csv(taxonomy_csv_path)
        db_session.close()
        if success:
            logger.info(f"Taxonomy initialization: {message}")
        else:
            logger.warning(f"Taxonomy initialization failed: {message}")

    logger.info("Application startup complete")
    yield

    # Shutdown
    logger.info("Shutting down application...")


# Create FastAPI app
app = FastAPI(
    title="Document Catalog",
    description="AI-powered document processing and search system",
    version="2.0.0",
    lifespan=lifespan,
)

# Add session middleware - MUST be added before other middleware that uses sessions
session_secret = settings.session_secret_key

# Validate session secret with more robust handling
if not session_secret:
    if settings.debug or not settings.require_app_auth:
        # Generate a temporary secret for development or when auth is disabled
        import secrets

        session_secret = secrets.token_urlsafe(32)
        logger.warning(
            "SESSION_SECRET_KEY not set. Using temporary session secret. "
            "Set SESSION_SECRET_KEY environment variable for production."
        )
    else:
        # In production with auth enabled, generate a temporary secret but warn
        import secrets

        session_secret = secrets.token_urlsafe(32)
        logger.error(
            "CRITICAL: SESSION_SECRET_KEY not found in production environment! "
            "Using temporary session secret. This is NOT secure for production. "
            "Set SESSION_SECRET_KEY environment variable immediately."
        )
elif len(session_secret) < 32:
    logger.warning(
        f"SESSION_SECRET_KEY is too short ({len(session_secret)} chars). "
        "Recommend at least 32 characters for security."
    )

# Validate session timeout
session_timeout_seconds = settings.session_timeout_hours * 3600
if session_timeout_seconds <= 0:
    logger.warning("Invalid session timeout, using default 24 hours")
    session_timeout_seconds = 24 * 3600

# Import Redis session middleware
from services.redis_session_middleware import (
    RedisSessionMiddleware,
    FallbackSessionMiddleware,
)
from services.redis_session_service import redis_session_service
from services.authentication_middleware import AuthenticationMiddleware

# Global flag to track if Redis session middleware is properly installed
redis_session_middleware_installed = False
session_middleware_error = None


def prepare_redis_session_middleware():
    """Prepare Redis session middleware config (but don't add it yet)"""
    global redis_session_middleware_installed, session_middleware_error

    try:
        # Validate session secret before attempting to initialize
        if not session_secret:
            raise ValueError("Session secret is empty or None")

        if len(session_secret) < 16:
            raise ValueError(
                f"Session secret too short: {len(session_secret)} chars (minimum 16)"
            )

        # Validate timeout
        if session_timeout_seconds <= 0:
            raise ValueError(
                f"Invalid session timeout: {session_timeout_seconds} seconds"
            )

        logger.info(
            f"Preparing Redis Session Middleware - "
            f"Secret length: {len(session_secret)}, "
            f"Timeout: {settings.session_timeout_hours}h, "
            f"Environment: {settings.environment}"
        )

        # Check Redis health first
        redis_health = redis_session_service.health_check()
        if redis_health["status"] != "healthy":
            raise ValueError(
                f"Redis session service not healthy: {redis_health.get('error', 'Unknown error')}"
            )

        # Configure Redis session middleware
        config = {
            "secret_key": session_secret,
            "max_age": session_timeout_seconds,
            "same_site": "lax",
            "https_only": not settings.debug,
        }

        redis_session_middleware_installed = True
        logger.info("Redis Session Middleware config prepared successfully")
        return True, config

    except Exception as e:
        session_middleware_error = str(e)
        logger.error(f"CRITICAL: Redis Session Middleware preparation failed: {e}")
        logger.error(f"Session secret present: {bool(session_secret)}")
        logger.error(
            f"Session secret length: {len(session_secret) if session_secret else 0}"
        )
        logger.error(f"Session timeout: {session_timeout_seconds}")
        logger.error(f"Environment: {settings.environment}")
        logger.error(f"Debug mode: {settings.debug}")

        redis_session_middleware_installed = False

        logger.warning(
            "Redis session middleware preparation failed - will use fallback"
        )
        return False, None


# Shared limiter instance imported from api/dependencies.py.
# Registering the same object in app.state ensures SlowAPIMiddleware and
# all @limiter.limit decorators (across every router file) use one backend.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CRITICAL: Middleware added via app.add_middleware() executes in REVERSE order
# Last added = First executed. So we add in reverse order of desired execution:

# Add rate limiting middleware FIRST (will execute LAST)
app.add_middleware(SlowAPIMiddleware)

# Add CORS middleware SECOND (will execute SECOND-TO-LAST).
# Origins are read from ALLOWED_ORIGINS env var (comma-separated).
# allow_origins=["*"] is intentionally avoided — browsers block credentialed
# requests to wildcard origins. See docs/architecture-fixes/FIX-004.
_allowed_origins = settings.get_allowed_origins_list()
if not _allowed_origins:
    logger.warning(
        "ALLOWED_ORIGINS is not configured. CORS is disabled for cross-origin requests. "
        "Set ALLOWED_ORIGINS=https://your-domain.onrender.com to enable."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
)

# Prepare Redis session middleware config (but don't add it yet)
session_init_success, session_config = prepare_redis_session_middleware()

# Add authentication middleware THIRD (will execute SECOND, after session loads)
logger.info(
    f"Adding Authentication Middleware - redis_session_installed: {redis_session_middleware_installed}"
)
app.add_middleware(
    AuthenticationMiddleware,
    redis_session_middleware_installed=redis_session_middleware_installed,
)

# Add session middleware LAST (will execute FIRST)
# This is CRITICAL - middleware added last executes first!
if session_init_success and session_config:
    logger.info("Adding Redis Session Middleware (will execute FIRST)")
    app.add_middleware(RedisSessionMiddleware, **session_config)
else:
    logger.warning("Adding fallback session middleware due to Redis session failure")
    app.add_middleware(FallbackSessionMiddleware)
    redis_session_middleware_installed = False

# Publish session state to the shared app_state so routers can read it
# without importing from main.py (which would create a circular import).
app_state.redis_session_middleware_installed = redis_session_middleware_installed
app_state.session_middleware_error = session_middleware_error


# Helper function for non-cacheable redirects
def create_redirect(url: str, status_code: int = 302) -> RedirectResponse:
    """Create a redirect response with cache prevention headers"""
    response = RedirectResponse(url=url, status_code=status_code)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# Add security headers middleware
@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses"""
    response = await call_next(request)

    # Add security headers
    security_headers = security_service.get_security_headers()
    for header, value in security_headers.items():
        response.headers[header] = value

    return response


# Add performance monitoring middleware
@app.middleware("http")
async def performance_monitoring_middleware(request: Request, call_next):
    """Monitor request performance and log slow queries"""
    start_time = time.time()

    response = await call_next(request)

    process_time = time.time() - start_time
    response.headers["X-Process-Time"] = str(process_time)

    # Log slow requests (>2 seconds)
    if process_time > 2.0:
        logger.warning(
            f"Slow request: {request.method} {request.url.path} took {process_time:.2f}s"
        )

    return response


# Mount static files
app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(dashboard_router, prefix="/api", tags=["Dashboard"])
app.include_router(documents_router, prefix="/api", tags=["Documents"])
app.include_router(search_router, prefix="/api", tags=["Search"])
app.include_router(taxonomy_router, prefix="/api", tags=["Taxonomy"])
app.include_router(review_router, prefix="/api", tags=["Review"])
app.include_router(admin_router, prefix="/api", tags=["Admin"])

# Mount files directory for serving uploaded documents
if settings.storage_type == "local":

    @app.get("/files/{filename}")
    async def serve_file(filename: str, authorization: Optional[str] = Header(None)):
        """Serve uploaded files for local storage - CONDITIONALLY SECURED"""
        # Only verify authentication if explicitly required
        if settings.require_auth:
            security_service.verify_api_key(authorization)

        # Sanitize filename to prevent path traversal
        safe_filename = security_service.sanitize_filename(filename)

        # Validate file path
        safe_path = security_service.validate_file_path(
            safe_filename, settings.storage_path
        )

        if os.path.exists(safe_path):
            return FileResponse(safe_path)
        raise HTTPException(status_code=404, detail="File not found")


# Templates
templates = Jinja2Templates(directory="templates")


# Dependency to get services
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


# This endpoint will now handle previews for all storage types
@app.get("/previews/{filename}")
async def serve_preview(
    filename: str,
    storage_service: StorageService = Depends(get_storage_service),
):
    """Serve preview images - PROTECTED by authentication middleware with failsafe streaming"""
    from fastapi.responses import StreamingResponse, RedirectResponse
    import io

    # Sanitize filename to prevent path traversal
    # Note: Authentication is enforced by authentication_middleware
    safe_filename = security_service.sanitize_filename(filename)
    preview_path = f"previews/{safe_filename}"

    try:
        # For S3 storage, try direct URL first for optimal performance
        if storage_service.storage_type == "s3" and settings.use_direct_urls:
            direct_url = await storage_service.get_file_url(
                preview_path, content_type="image/png"
            )
            if direct_url:
                logger.debug(f"Redirecting preview {safe_filename} to direct URL")
                # Create redirect response with cache headers to avoid regenerating presigned URLs
                response = RedirectResponse(url=direct_url, status_code=302)
                # Cache the redirect for 1 hour (matches presigned URL expiration)
                response.headers["Cache-Control"] = "public, max-age=3600"
                # Add Expires header for better browser compatibility
                expires_time = datetime.utcnow() + timedelta(hours=1)
                response.headers["Expires"] = expires_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
                return response
            else:
                logger.warning(
                    f"Failed to generate presigned URL for {safe_filename}, falling back to streaming"
                )

        # Fallback: Stream the file directly from storage
        # This happens for local storage, when direct URLs are disabled, or if presigned URL generation fails
        file_content = await storage_service.get_file(preview_path)
        if file_content:
            logger.debug(f"Streaming preview {safe_filename} directly from storage")
            return StreamingResponse(
                io.BytesIO(file_content),
                media_type="image/png",
                headers={
                    "Cache-Control": "public, max-age=86400",
                    "Content-Disposition": "inline",
                },
            )
        else:
            logger.warning(f"Preview file not found in storage: {preview_path}")

    except Exception as e:
        logger.error(f"Error serving preview for {safe_filename}: {e}", exc_info=True)

    # If the file is not found or an error occurs, return a placeholder
    placeholder_path = "static/placeholder.svg"
    if os.path.exists(placeholder_path):
        logger.debug(f"Serving placeholder for missing preview: {safe_filename}")
        return FileResponse(placeholder_path, media_type="image/svg+xml")

    raise HTTPException(status_code=404, detail="Preview not found")


# Routes


# Authentication routes
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/", error: str = None):
    """Login page - session-independent when middleware unavailable"""
    logger.info(
        f"Login page accessed - redis_session_middleware_installed: {redis_session_middleware_installed}, error: {error}"
    )

    # If sessions are not available, show error message
    if not redis_session_middleware_installed:
        logger.info("Showing login page with session unavailable error")
        error_message = (
            "Session management is not available. Please check server configuration."
        )
        if error == "session_unavailable":
            error_message = (
                "Session system is unavailable. Please contact administrator."
            )
        elif error == "session_failed":
            error_message = "Session system failed. Please contact administrator."
        elif error == "config":
            error_message = (
                "Authentication not properly configured. Please contact administrator."
            )

        logger.info(f"Returning template with error: {error_message}")
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "next": next,
                "error": error_message,
            },
        )

    # If sessions ARE available, check if user is already authenticated
    logger.info("Redis session middleware is installed, checking session validity")
    try:
        if security_service.is_session_valid(request):
            logger.info(f"User already authenticated, redirecting to {next}")
            return create_redirect(next)
    except Exception as e:
        logger.warning(f"Session validation error in login page: {e}")
        # Don't redirect on error, just show the login page
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "next": next,
                "error": "Session error occurred. Please try logging in again.",
            },
        )

    logger.info("Showing normal login page")
    return templates.TemplateResponse("login.html", {"request": request, "next": next})


@app.post("/login")
@limiter.limit("10/minute")  # Rate limit login attempts
async def login_submit(
    request: Request,
    password: str = Form(...),
    remember_me: bool = Form(False),
    next: str = Form("/"),
):
    """Process login form"""
    try:
        # Check if sessions are available
        if not redis_session_middleware_installed:
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "error": "Session management is not available. Please check server configuration.",
                    "next": next,
                },
            )

        # Verify password
        if not security_service.verify_app_password(password):
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "error": "Invalid password. Please try again.",
                    "next": next,
                },
            )

        # Create session
        try:
            security_service.create_session(request)

            # If remember me is checked, extend session
            if remember_me:
                # Extend session to 30 days
                request.session["auth_timestamp"] = datetime.now().isoformat()
                # Note: We could implement a separate "remember me" token system here

            # Redirect to intended page
            return create_redirect(next)
        except Exception as session_error:
            logger.error(f"Session creation error: {session_error}")
            return templates.TemplateResponse(
                "login.html",
                {
                    "request": request,
                    "error": "Unable to create session. Please try again or contact administrator.",
                    "next": next,
                },
            )

    except Exception as e:
        logger.error(f"Login error: {str(e)}")
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "An error occurred during login. Please try again.",
                "next": next,
            },
        )


@app.get("/logout")
async def logout(request: Request):
    """Logout and destroy session"""
    security_service.destroy_session(request)
    return create_redirect("/login")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page - redirect to search"""
    # Check authentication if required
    if settings.require_app_auth and redis_session_middleware_installed:
        try:
            if not security_service.is_session_valid(request):
                return create_redirect("/login")
        except Exception as e:
            logger.error(f"Authentication check error on home page: {e}")
            # If there's an error checking auth, redirect to login to be safe
            return create_redirect("/login")

    return templates.TemplateResponse("search.html", {"request": request})


@app.get("/health")
async def health_check():
    """Health check endpoint for Render"""
    return {"status": "healthy", "version": "2.0.0"}


@app.get("/health/storage")
async def storage_health_check(
    storage_service: StorageService = Depends(get_storage_service),
):
    """Storage and direct URL optimization health check"""
    try:
        health_info = {
            "storage_type": storage_service.storage_type,
            "direct_urls_enabled": settings.use_direct_urls,
            "preview_url_expires_hours": settings.preview_url_expires_hours,
            "download_url_expires_hours": settings.download_url_expires_hours,
            "timestamp": datetime.now().isoformat(),
        }

        # Test direct URL generation if using S3
        if storage_service.storage_type == "s3":
            try:
                # Test presigned URL generation with a dummy key
                test_url = storage_service._get_s3_presigned_url(
                    "test/dummy.pdf", expires_in=60
                )
                health_info["s3_presigned_url_generation"] = (
                    "working" if test_url else "failed"
                )
                health_info["s3_bucket"] = settings.s3_bucket
                health_info["s3_region"] = settings.s3_region
            except Exception as e:
                health_info["s3_presigned_url_generation"] = f"error: {str(e)}"

        # Overall status
        if storage_service.storage_type == "s3" and settings.use_direct_urls:
            if health_info.get("s3_presigned_url_generation") == "working":
                status = "optimized"
            else:
                status = "degraded"
        else:
            status = "basic"

        return {
            "status": status,
            "optimization_active": storage_service.storage_type == "s3"
            and settings.use_direct_urls,
            "storage_health": health_info,
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        }


@app.get("/health/session")
async def session_health_check(request: Request):
    """Simplified session health check endpoint"""
    try:
        # Check if SessionMiddleware is working
        session_available = hasattr(request, "session")

        # Basic health info
        health_info = {
            "redis_session_middleware_installed": redis_session_middleware_installed,
            "session_middleware_available": session_available,
            "session_middleware_error": session_middleware_error,
            "require_app_auth": settings.require_app_auth,
            "environment": settings.environment,
            "session_secret_configured": bool(settings.session_secret_key),
            "app_password_configured": bool(settings.app_password),
        }

        # Add Redis session service health
        try:
            redis_health = redis_session_service.health_check()
            health_info["redis_session_service"] = redis_health
        except Exception as e:
            health_info["redis_session_service"] = {"status": "error", "error": str(e)}

        # Simple session accessibility test
        if session_available:
            try:
                # Just try to access the session without detailed inspection
                _ = dict(request.session)
                health_info["session_accessible"] = True
            except Exception as e:
                health_info["session_accessible"] = False
                health_info["session_error"] = str(e)

        # Determine overall status
        if redis_session_middleware_installed and session_available:
            status = "healthy"
        else:
            status = "error"

        return {
            "status": status,
            "session_health": health_info,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now().isoformat(),
        }


# /api/admin/clear-cache → api/admin.py

# Document Upload
@app.post("/api/documents/upload")
@limiter.limit("20/minute")  # Increased rate limit for better usability
async def upload_documents(
    request: Request,
    files: List[UploadFile] = File(...),
    password: str = Form(""),
    document_service: DocumentService = Depends(get_document_service),
    storage_service: StorageService = Depends(get_storage_service),
    background_tasks: BackgroundTasks = None,
):
    """Upload one or more documents - SIMPLE PASSWORD PROTECTION"""
    try:
        # Simple password check
        upload_password = settings.upload_password or "upload123"
        if not password or password != upload_password:
            raise HTTPException(status_code=401, detail="Invalid upload password")

        tasks = []
        delay_seconds = 30  # 30 seconds between each document

        for i, file in enumerate(files):
            if not file.filename:
                continue

            # Basic filename sanitization only
            safe_filename = security_service.sanitize_filename(file.filename)

            # Save file to storage
            file_path = await storage_service.save_file(file)

            # Create document record
            document = await document_service.create_document(
                filename=safe_filename, file_path=file_path, file_size=file.size or 0
            )

            # Dispatch Celery task for processing with a staggered delay
            countdown = i * delay_seconds
            task = process_document_task.apply_async(
                args=[document.id], countdown=countdown
            )

            tasks.append(
                {
                    "document_id": document.id,
                    "filename": document.filename,
                    "task_id": task.id,
                    "processing_starts_in_seconds": countdown,
                }
            )

        # Return results
        response = {
            "success": len(tasks) > 0,
            "message": f"Queued {len(tasks)} documents for processing",
            "tasks": tasks,
        }

        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Upload error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# /api/documents/search and /api/search/* routes → api/search.py


# Get Document
@app.get("/api/documents/{document_id}")
async def get_document(
    document_id: int, document_service: DocumentService = Depends(get_document_service)
):
    """Get document by ID"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        return {"success": True, "document": document.to_dict(full_detail=True)}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Get document error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))



# /api/facets/* and /api/review/* routes → api/review.py

# Document Preview
@app.get("/api/documents/{document_id}/preview")
async def get_document_preview(
    document_id: int,
    document_service: DocumentService = Depends(get_document_service),
    preview_service: PreviewService = Depends(get_preview_service),
):
    """Get document preview"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        preview_url = await preview_service.get_preview_url(document.file_path)

        return {
            "success": True,
            "preview_url": preview_url,
            "filename": document.filename,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Preview error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Bulk Document Download
@app.post("/api/documents/bulk-download")
async def bulk_download_documents(
    request: Request,
    document_service: DocumentService = Depends(get_document_service),
    storage_service: StorageService = Depends(get_storage_service),
):
    """Download multiple documents as a single ZIP archive"""
    import zipfile
    import io
    from fastapi.responses import StreamingResponse

    try:
        body = await request.json()
        document_ids = body.get("document_ids", [])

        if not document_ids:
            raise HTTPException(status_code=400, detail="No document IDs provided")
        if len(document_ids) > 100:
            raise HTTPException(status_code=400, detail="Too many documents (max 100)")

        zip_buffer = io.BytesIO()
        seen_names: dict[str, int] = {}
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for doc_id in document_ids:
                document = await document_service.get_document(doc_id)
                if not document:
                    continue
                file_content = await storage_service.get_file(document.file_path)
                if not file_content:
                    continue
                # Deduplicate filenames inside the archive
                name = document.filename
                if name in seen_names:
                    seen_names[name] += 1
                    stem, _, ext = name.rpartition(".")
                    name = f"{stem}_{seen_names[name]}.{ext}" if ext else f"{name}_{seen_names[name]}"
                else:
                    seen_names[name] = 0
                zf.writestr(name, file_content)

        zip_buffer.seek(0)
        return StreamingResponse(
            zip_buffer,
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=documents.zip"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Bulk download error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Document Download
@app.get("/api/documents/{document_id}/download")
async def download_document(
    document_id: int,
    document_service: DocumentService = Depends(get_document_service),
    storage_service: StorageService = Depends(get_storage_service),
):
    """Download a document file - redirects to direct URLs for S3, streams for local storage"""
    from fastapi.responses import StreamingResponse, RedirectResponse
    import io

    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        # For S3 storage, redirect to direct URL for optimal performance
        if storage_service.storage_type == "s3" and settings.use_direct_urls:
            direct_url = await storage_service.get_file_url(
                document.file_path, content_type="application/pdf"
            )
            if direct_url:
                logger.debug(f"Redirecting download {document.filename} to direct URL")
                return RedirectResponse(url=direct_url, status_code=302)

        # For local storage or when direct URLs are disabled, stream the file
        file_content = await storage_service.get_file(document.file_path)
        if not file_content:
            raise HTTPException(status_code=404, detail="File not found in storage")

        return StreamingResponse(
            io.BytesIO(file_content),
            media_type="application/pdf",
            headers={"Content-Disposition": f"inline; filename={document.filename}"},
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Download error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Processing Status
@app.get("/api/documents/{document_id}/status")
async def get_processing_status(
    document_id: int, document_service: DocumentService = Depends(get_document_service)
):
    """Get document processing status"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        return {
            "success": True,
            "status": document.status,
            "progress": document.processing_progress or 0,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Status error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Search Interface
@app.get("/search", response_class=HTMLResponse)
async def search_page(request: Request):
    """Search page"""
    return templates.TemplateResponse("search.html", {"request": request})


# Upload Interface
@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    """Upload page"""
    return templates.TemplateResponse("upload.html", {"request": request})


# Admin Interface
@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin dashboard"""
    return templates.TemplateResponse("admin/dashboard.html", {"request": request})


@app.get("/review/dates", response_class=HTMLResponse)
async def review_dates_page(request: Request):
    """Human-in-the-loop date review queue"""
    return templates.TemplateResponse("review_dates.html", {"request": request})


# /api/taxonomy/* routes → api/taxonomy.py
# /api/ai/* routes → api/admin.py
# /api/tasks/* routes → api/admin.py
# /api/stats routes → api/admin.py

if __name__ == "__main__":
    import uvicorn
    import multiprocessing

    # Set the start method for multiprocessing
    multiprocessing.set_start_method("fork", force=True)

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", 8000)),
        reload=settings.debug,
    )


# Remaining routes moved to api/search.py, api/admin.py — see FIX-006.
