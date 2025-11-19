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
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import os
from typing import List, Optional
import logging
from sqlalchemy.orm import Session
from pathlib import Path
from datetime import datetime, timedelta

from config import get_settings
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
from worker import process_document_task
from celery.result import AsyncResult
from models.search_query import SearchQuery

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
    await init_db()

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


# Initialize rate limiter (do this before adding middleware)
limiter = Limiter(key_func=get_remote_address, default_limits=["1000/hour"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CRITICAL: Middleware added via app.add_middleware() executes in REVERSE order
# Last added = First executed. So we add in reverse order of desired execution:

# Add rate limiting middleware FIRST (will execute LAST)
app.add_middleware(SlowAPIMiddleware)

# Add CORS middleware SECOND (will execute SECOND-TO-LAST)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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
) -> SearchService:
    return SearchService(db, preview_service)


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
                return RedirectResponse(url=direct_url, status_code=302)
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


@app.post("/api/admin/clear-cache")
async def clear_redis_cache(password: str = Form(...)):
    """Clear Redis search and facet caches - Admin only"""
    import redis as redis_lib

    try:
        # Verify admin password
        admin_password = settings.upload_password or "upload123"
        if password != admin_password:
            raise HTTPException(status_code=401, detail="Invalid password")

        # Connect to Redis
        if not settings.redis_url:
            raise HTTPException(status_code=500, detail="Redis not configured")

        redis_client = redis_lib.from_url(settings.redis_url, decode_responses=True)
        redis_client.ping()

        # Get all cache keys
        search_keys = redis_client.keys("search:*")
        facet_keys = redis_client.keys("facets:*")
        all_keys = search_keys + facet_keys

        if not all_keys:
            return {
                "success": True,
                "message": "Cache already empty",
                "deleted_count": 0,
                "search_keys": 0,
                "facet_keys": 0,
            }

        # Delete all cache keys
        deleted = redis_client.delete(*all_keys)

        logger.info(f"Cleared {deleted} cache keys from Redis")

        return {
            "success": True,
            "message": f"Successfully cleared {deleted} cache entries",
            "deleted_count": deleted,
            "search_keys": len(search_keys),
            "facet_keys": len(facet_keys),
            "use_direct_urls": settings.use_direct_urls,
            "storage_type": settings.storage_type,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error clearing cache: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


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
        delay_seconds = 120  # 2 minutes

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


# Document Search
@app.get("/api/documents/search")
@limiter.limit("30/minute")
async def search_documents(
    request: Request,
    q: str = "",
    page: int = 1,
    per_page: int = 20,
    primary_category: Optional[str] = None,
    subcategory: Optional[str] = None,
    canonical_term: Optional[str] = None,
    sort_by: str = "relevance",
    sort_direction: str = "desc",
    search_service: SearchService = Depends(get_search_service),
):
    """Search documents - PUBLIC ENDPOINT"""
    try:
        # Validate and sanitize search query
        safe_query = security_service.validate_search_query(q)

        # Validate pagination parameters
        if page < 1:
            page = 1
        if per_page < 1 or per_page > 100:
            per_page = 20

        # Validate sort parameters
        allowed_sort_fields = [
            "relevance",
            "created_at",
            "updated_at",
            "filename",
            "file_size",
        ]
        if sort_by not in allowed_sort_fields:
            sort_by = "relevance"

        if sort_direction.lower() not in ["asc", "desc"]:
            sort_direction = "desc"

        results = await search_service.search(
            query=safe_query,
            page=page,
            per_page=per_page,
            primary_category=primary_category,
            subcategory=subcategory,
            canonical_term=canonical_term,
            sort_by=sort_by,
            sort_direction=sort_direction,
        )

        return {
            "success": True,
            "documents": results["documents"],
            "pagination": results["pagination"],
            "facets": results["facets"],
            "total_count": results["total_count"],
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


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


# Reprocess Document
@app.post("/api/documents/{document_id}/reprocess")
async def reprocess_document(
    document_id: int,
    analysis_type: str = "unified",
    document_service: DocumentService = Depends(get_document_service),
):
    """Reprocess a document with optional analysis type"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        # Reset status
        await document_service.update_document_status(document_id, "PENDING")

        # Queue for reprocessing with Celery
        task = process_document_task.delay(document.id, analysis_type)

        return {
            "success": True,
            "message": f"Document {document.filename} queued for reprocessing with {analysis_type} analysis",
            "task_id": task.id,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Reprocess error: {str(e)}")
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


# Taxonomy API Endpoints
@app.get("/api/taxonomy/categories")
async def get_taxonomy_categories(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get all primary categories from taxonomy"""
    try:
        categories = await taxonomy_service.get_primary_categories()
        return {"success": True, "categories": categories}

    except Exception as e:
        logger.error(f"Taxonomy categories error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/categories/{primary_category}/subcategories")
async def get_taxonomy_subcategories(
    primary_category: str,
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get subcategories for a primary category"""
    try:
        subcategories = await taxonomy_service.get_subcategories(primary_category)
        return {"success": True, "subcategories": subcategories}

    except Exception as e:
        logger.error(f"Taxonomy subcategories error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/hierarchy")
async def get_taxonomy_hierarchy(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get complete taxonomy hierarchy"""
    try:
        hierarchy = await taxonomy_service.get_taxonomy_hierarchy()
        return {"success": True, "hierarchy": hierarchy}

    except Exception as e:
        logger.error(f"Taxonomy hierarchy error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/filter-data")
async def get_filter_taxonomy(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get taxonomy data structured for UI filters"""
    try:
        filter_data = await taxonomy_service.get_filter_taxonomy_data()
        return {"success": True, "data": filter_data}
    except Exception as e:
        logger.error(f"Filter taxonomy data error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/canonical-terms")
async def get_canonical_terms(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get a flat list of all canonical terms"""
    try:
        terms = await taxonomy_service.get_all_canonical_terms()
        return {"success": True, "terms": terms}
    except Exception as e:
        logger.error(f"Error getting canonical terms: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


@app.get("/api/taxonomy/search")
async def search_taxonomy_terms(
    q: str,
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Search taxonomy terms"""
    try:
        terms = await taxonomy_service.search_terms(q)
        return {"success": True, "terms": terms}

    except Exception as e:
        logger.error(f"Taxonomy search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/taxonomy/stats")
async def get_taxonomy_statistics(
    taxonomy_service: TaxonomyService = Depends(get_taxonomy_service),
):
    """Get taxonomy statistics"""
    try:
        stats = await taxonomy_service.get_statistics()
        return {"success": True, "stats": stats}

    except Exception as e:
        logger.error(f"Taxonomy stats error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# AI Service API Endpoints
@app.get("/api/ai/info")
async def get_ai_info(
    ai_service: AIService = Depends(get_ai_service),
):
    """Get AI service configuration and capabilities"""
    try:
        info = ai_service.get_ai_info()
        return {"success": True, "ai_info": info}

    except Exception as e:
        logger.error(f"AI info error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/ai/analysis-types")
async def get_analysis_types(
    ai_service: AIService = Depends(get_ai_service),
):
    """Get available analysis types"""
    try:
        types = ai_service.get_available_analysis_types()
        return {"success": True, "analysis_types": types}

    except Exception as e:
        logger.error(f"Analysis types error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/documents/{document_id}/analyze")
async def analyze_document_with_type(
    document_id: int,
    analysis_type: str = "unified",
    ai_service: AIService = Depends(get_ai_service),
    document_service: DocumentService = Depends(get_document_service),
    storage_service: StorageService = Depends(get_storage_service),
):
    """Perform immediate analysis on a document with specified type"""
    try:
        # Get document
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

        # Perform analysis
        result = await ai_service.analyze_document(
            document.file_path, document.filename, analysis_type
        )

        return {
            "success": True,
            "document_id": document_id,
            "analysis_type": analysis_type,
            "result": result,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Document analysis error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# Statistics API
@app.get("/api/tasks/{task_id}/status")
async def get_task_status(task_id: str):
    """Get the status of a Celery task"""
    task_result = AsyncResult(task_id)
    result = {
        "task_id": task_id,
        "status": task_result.status,
        "result": task_result.result,
    }
    return {"success": True, "task": result}


@app.get("/api/stats")
async def get_statistics(
    document_service: DocumentService = Depends(get_document_service),
):
    """Get application statistics"""
    try:
        stats = await document_service.get_statistics()
        return {"success": True, "stats": stats}

    except Exception as e:
        logger.error(f"Stats error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


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


@app.get("/api/search/canonical/{canonical_term}")
async def search_by_canonical_term(
    canonical_term: str,
    q: str = "",
    search_service: SearchService = Depends(get_search_service),
):
    """Search documents by canonical term with optional query"""
    try:
        results = await search_service.search_by_canonical_term(canonical_term, query=q)
        return {"success": True, "documents": results}
    except Exception as e:
        logger.error(f"Canonical term search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search/verbatim/{verbatim_term}")
async def search_by_verbatim_term(
    verbatim_term: str,
    q: str = "",
    search_service: SearchService = Depends(get_search_service),
):
    """Search documents by verbatim term with optional query"""
    try:
        results = await search_service.search_by_verbatim_term(verbatim_term, query=q)
        return {"success": True, "documents": results}
    except Exception as e:
        logger.error(f"Verbatim term search error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/documents/{document_id}/mappings")
async def get_document_mappings(
    document_id: int,
    document_service: DocumentService = Depends(get_document_service),
):
    """Get keyword mappings for a document"""
    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
        mappings = document.get_keyword_mappings()
        return {"success": True, "mappings": mappings}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Document mappings error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/stats/mappings")
async def get_mapping_stats(
    search_service: SearchService = Depends(get_search_service),
):
    """Get statistics about keyword mappings"""
    try:
        stats = await search_service.get_mapping_statistics()
        return {"success": True, "stats": stats}
    except Exception as e:
        logger.error(f"Mapping stats error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search/top-queries")
async def get_top_queries(
    search_service: SearchService = Depends(get_search_service),
):
    """Get top 8 search queries"""
    try:
        queries = await search_service.get_top_queries(limit=8)
        return {"success": True, "queries": queries}
    except Exception as e:
        logger.error(f"Top queries error: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))
