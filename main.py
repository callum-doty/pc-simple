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
from starlette.middleware.sessions import SessionMiddleware
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

# Global flag to track if SessionMiddleware is properly installed
session_middleware_installed = False
session_middleware_error = None


def initialize_session_middleware():
    """Initialize SessionMiddleware with comprehensive error handling and validation"""
    global session_middleware_installed, session_middleware_error

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
            f"Initializing SessionMiddleware - "
            f"Secret length: {len(session_secret)}, "
            f"Timeout: {settings.session_timeout_hours}h, "
            f"Secure cookies: {not settings.debug}, "
            f"Environment: {settings.environment}"
        )

        # Add SessionMiddleware first - this is critical for session access
        app.add_middleware(
            SessionMiddleware,
            secret_key=session_secret,
            max_age=session_timeout_seconds,
            same_site="lax",
            https_only=not settings.debug,  # Use secure cookies in production
        )

        session_middleware_installed = True
        logger.info("SessionMiddleware initialized successfully")
        return True

    except Exception as e:
        session_middleware_error = str(e)
        logger.error(f"CRITICAL: Failed to initialize SessionMiddleware: {e}")
        logger.error(f"Session secret present: {bool(session_secret)}")
        logger.error(
            f"Session secret length: {len(session_secret) if session_secret else 0}"
        )
        logger.error(f"Session timeout: {session_timeout_seconds}")
        logger.error(f"Environment: {settings.environment}")
        logger.error(f"Debug mode: {settings.debug}")

        session_middleware_installed = False

        # In production, this is a critical error that should prevent startup
        if settings.environment == "production" and settings.require_app_auth:
            logger.error(
                "FATAL: SessionMiddleware failed to initialize in production with auth enabled. "
                "This is a critical security issue. Application cannot start safely."
            )
            # Don't raise here - let the app start but show clear error messages

        logger.warning("Application will start without session support")
        return False


# Initialize session middleware
initialize_session_middleware()

# Add CORS middleware early in the stack (after SessionMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Initialize rate limiter
limiter = Limiter(key_func=get_remote_address, default_limits=["1000/hour"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Add rate limiting middleware
app.add_middleware(SlowAPIMiddleware)


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


@app.middleware("http")
async def authentication_middleware(request: Request, call_next):
    """Check authentication for protected routes with SECURE fail-closed handling"""
    # Skip authentication for certain paths
    skip_auth_paths = [
        "/login",
        "/health",
        "/static",
        "/favicon.ico",
    ]

    # Check if authentication is required
    if not settings.require_app_auth:
        logger.debug("Authentication disabled via REQUIRE_APP_AUTH=false")
        response = await call_next(request)
        return response

    # Skip authentication for allowed paths
    if any(request.url.path.startswith(path) for path in skip_auth_paths):
        response = await call_next(request)
        return response

    # SECURITY FIRST: Check if APP_PASSWORD is configured
    if not settings.app_password:
        logger.error(
            "CRITICAL SECURITY ISSUE: APP_PASSWORD not configured but REQUIRE_APP_AUTH=true. "
            "Denying access to protect the application."
        )
        if request.method == "GET" and not request.url.path.startswith("/api"):
            return RedirectResponse(url="/login?error=config", status_code=302)
        else:
            raise HTTPException(
                status_code=503,
                detail="Authentication not properly configured. Please contact administrator.",
            )

    # CRITICAL FIX: Handle SessionMiddleware issues without creating redirect loops
    try:
        # Test if we can access request.session at all
        if not hasattr(request, "session"):
            raise AssertionError(
                "SessionMiddleware not installed - request.session not available"
            )

        # Test basic session access - this will raise AssertionError if SessionMiddleware failed
        _ = dict(request.session)

    except (AssertionError, AttributeError) as session_error:
        logger.error(
            f"CRITICAL SESSION ISSUE: {session_error}. "
            "SessionMiddleware failed to install properly on Render."
        )

        # PREVENT REDIRECT LOOP: For session errors, show a static error page instead of redirecting
        if request.method == "GET" and not request.url.path.startswith("/api"):
            # Return a simple HTML error page instead of redirecting
            error_html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Service Unavailable - Document Catalog</title>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background: #f5f5f5; }
                    .error-container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 500px; margin: 0 auto; }
                    .error-icon { font-size: 48px; color: #dc3545; margin-bottom: 20px; }
                    h1 { color: #333; margin-bottom: 20px; }
                    p { color: #666; line-height: 1.6; margin-bottom: 20px; }
                    .error-code { background: #f8f9fa; padding: 10px; border-radius: 5px; font-family: monospace; color: #495057; margin: 20px 0; }
                </style>
            </head>
            <body>
                <div class="error-container">
                    <div class="error-icon">⚠️</div>
                    <h1>Service Temporarily Unavailable</h1>
                    <p>The Document Catalog application is experiencing a configuration issue and cannot start properly.</p>
                    <div class="error-code">Session management system failed to initialize</div>
                    <p>This is typically a deployment configuration issue. Please contact the administrator or try again in a few minutes.</p>
                    <p><strong>Error:</strong> SessionMiddleware installation failed</p>
                </div>
            </body>
            </html>
            """
            return HTMLResponse(content=error_html, status_code=503)
        else:
            # For API calls, return JSON error
            raise HTTPException(
                status_code=503,
                detail="Session management system failed to initialize. This is a deployment configuration issue.",
            )

    # SECURE session validation with fail-closed error handling
    try:
        # Now try the security service validation
        try:
            is_valid = security_service.is_session_valid(request)
        except Exception as security_service_error:
            logger.error(f"Security service validation error: {security_service_error}")
            # FAIL CLOSED: If security service fails, deny access
            is_valid = False

        # If not authenticated, redirect to login
        if not is_valid:
            logger.debug("User not authenticated, redirecting to login")
            # Store the original URL for redirect after login
            if request.method == "GET" and not request.url.path.startswith("/api"):
                # For HTML pages, redirect to login
                return RedirectResponse(
                    url=f"/login?next={request.url.path}", status_code=302
                )
            else:
                # For API calls, return 401
                raise HTTPException(status_code=401, detail="Authentication required")

        # If we get here, user is authenticated, proceed with request
        response = await call_next(request)
        return response

    except HTTPException:
        # Re-raise HTTP exceptions (like 401, 302 redirects) - these are expected
        raise
    except Exception as e:
        logger.error(f"Unexpected authentication middleware error: {e}")
        logger.error(f"Error type: {type(e).__name__}")
        logger.error(f"Request path: {request.url.path}")
        logger.error(f"Request method: {request.method}")

        # PREVENT REDIRECT LOOPS: For unexpected errors, show error page instead of redirecting
        if request.method == "GET" and not request.url.path.startswith("/api"):
            error_html = """
            <!DOCTYPE html>
            <html>
            <head>
                <title>Authentication Error - Document Catalog</title>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <style>
                    body { font-family: Arial, sans-serif; text-align: center; padding: 50px; background: #f5f5f5; }
                    .error-container { background: white; padding: 40px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); max-width: 500px; margin: 0 auto; }
                    .error-icon { font-size: 48px; color: #dc3545; margin-bottom: 20px; }
                    h1 { color: #333; margin-bottom: 20px; }
                    p { color: #666; line-height: 1.6; margin-bottom: 20px; }
                </style>
            </head>
            <body>
                <div class="error-container">
                    <div class="error-icon">🔒</div>
                    <h1>Authentication System Error</h1>
                    <p>The authentication system encountered an unexpected error and cannot process your request.</p>
                    <p>Please try again in a few minutes or contact the administrator if the problem persists.</p>
                </div>
            </body>
            </html>
            """
            return HTMLResponse(content=error_html, status_code=503)
        else:
            raise HTTPException(
                status_code=503,
                detail="Authentication system error. Please contact administrator.",
            )


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
    """Serve preview images by streaming from the storage service - PUBLIC"""
    from fastapi.responses import StreamingResponse
    import io

    # Sanitize filename to prevent path traversal (but no auth required for previews)
    safe_filename = security_service.sanitize_filename(filename)
    preview_path = f"previews/{safe_filename}"

    try:
        file_content = await storage_service.get_file(preview_path)
        if file_content:
            return StreamingResponse(io.BytesIO(file_content), media_type="image/png")
    except Exception as e:
        logger.error(f"Error serving preview for {safe_filename}: {e}")

    # If the file is not found or an error occurs, return a placeholder
    placeholder_path = "static/placeholder.svg"
    if os.path.exists(placeholder_path):
        return FileResponse(placeholder_path, media_type="image/svg+xml")

    raise HTTPException(status_code=404, detail="Preview not found")


# Routes


# Authentication routes
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    """Login page"""
    # If sessions are not available, show a message
    if not session_middleware_installed:
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "next": next,
                "error": "Session management is not available. Please check server configuration.",
            },
        )

    # If already authenticated, redirect to intended page
    try:
        if security_service.is_session_valid(request):
            return RedirectResponse(url=next, status_code=302)
    except Exception as e:
        logger.warning(f"Session validation error in login page: {e}")

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
        if not session_middleware_installed:
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
            return RedirectResponse(url=next, status_code=302)
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
    return RedirectResponse(url="/login", status_code=302)


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Home page - redirect to search"""
    return templates.TemplateResponse("search.html", {"request": request})


@app.get("/health")
async def health_check():
    """Health check endpoint for Render"""
    return {"status": "healthy", "version": "2.0.0"}


@app.get("/health/session")
async def session_health_check(request: Request):
    """Enhanced session health check endpoint for debugging"""
    try:
        # Check if SessionMiddleware is working
        session_available = hasattr(request, "session")

        health_info = {
            "session_middleware_installed": session_middleware_installed,
            "session_middleware_available": session_available,
            "session_middleware_error": session_middleware_error,
            "require_app_auth": settings.require_app_auth,
            "session_timeout_hours": settings.session_timeout_hours,
            "session_secret_configured": bool(settings.session_secret_key),
            "session_secret_length": (
                len(settings.session_secret_key) if settings.session_secret_key else 0
            ),
            "environment": settings.environment,
            "debug_mode": settings.debug,
            "app_password_configured": bool(settings.app_password),
        }

        # Add environment variable diagnostics
        env_vars = {
            "SESSION_SECRET_KEY": bool(os.getenv("SESSION_SECRET_KEY")),
            "APP_PASSWORD": bool(os.getenv("APP_PASSWORD")),
            "REQUIRE_APP_AUTH": os.getenv("REQUIRE_APP_AUTH"),
            "ENVIRONMENT": os.getenv("ENVIRONMENT"),
            "RENDER": bool(os.getenv("RENDER")),
        }
        health_info["environment_variables"] = env_vars

        if session_available:
            # Try to access session data
            try:
                session_data = dict(request.session)
                health_info["session_accessible"] = True
                health_info["session_keys"] = list(session_data.keys())
                health_info["has_auth_token"] = "auth_token" in session_data
                health_info["has_auth_timestamp"] = "auth_timestamp" in session_data

                if "auth_timestamp" in session_data:
                    try:
                        auth_time = datetime.fromisoformat(
                            session_data["auth_timestamp"]
                        )
                        expiry_time = auth_time + timedelta(
                            hours=settings.session_timeout_hours
                        )
                        health_info["session_expires_at"] = expiry_time.isoformat()
                        health_info["session_expired"] = datetime.now() > expiry_time
                    except Exception as e:
                        health_info["timestamp_parse_error"] = str(e)

            except Exception as e:
                health_info["session_accessible"] = False
                health_info["session_error"] = str(e)

        # Determine overall status
        if session_middleware_installed and session_available:
            status = "healthy"
        elif session_middleware_installed and not session_available:
            status = "warning"
        else:
            status = "error"

        # Add recommendations based on status
        recommendations = []
        if not session_middleware_installed:
            recommendations.append(
                "SessionMiddleware failed to initialize - check logs for details"
            )
        if session_middleware_error:
            recommendations.append(f"Session error: {session_middleware_error}")
        if not settings.session_secret_key:
            recommendations.append("SESSION_SECRET_KEY environment variable not set")
        if settings.require_app_auth and not settings.app_password:
            recommendations.append(
                "APP_PASSWORD environment variable not set but authentication is required"
            )

        health_info["recommendations"] = recommendations

        return {
            "status": status,
            "session_health": health_info,
            "timestamp": datetime.now().isoformat(),
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e),
            "session_health": {"error": "Failed to check session health"},
            "timestamp": datetime.now().isoformat(),
        }


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
    """Download a document file"""
    from fastapi.responses import StreamingResponse
    import io

    try:
        document = await document_service.get_document(document_id)
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")

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
