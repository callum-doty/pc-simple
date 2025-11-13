"""
Authentication middleware for FastAPI application
Runs AFTER RedisSessionMiddleware to ensure session is loaded
"""

import logging
from typing import Callable
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.requests import Request
from starlette.responses import Response, RedirectResponse
from fastapi import HTTPException

from config import get_settings
from services.security_service import security_service

logger = logging.getLogger(__name__)
settings = get_settings()


class AuthenticationMiddleware:
    """
    Authentication middleware that checks session validity for protected routes.
    Must be added AFTER RedisSessionMiddleware to ensure session is loaded.
    """

    def __init__(
        self,
        app: ASGIApp,
        redis_session_middleware_installed: bool = True,
    ):
        self.app = app
        self.redis_session_middleware_installed = redis_session_middleware_installed

        # Whitelist of paths that don't require authentication
        self.skip_auth_paths = [
            "/login",
            "/health",
            "/static",
            "/favicon.ico",
        ]

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Create request object to access path and session
        request = Request(scope, receive)

        # Skip authentication for whitelisted paths
        if any(request.url.path.startswith(path) for path in self.skip_auth_paths):
            await self.app(scope, receive, send)
            return

        # Check if authentication is required
        if not settings.require_app_auth:
            logger.debug("Authentication disabled via REQUIRE_APP_AUTH=false")
            await self.app(scope, receive, send)
            return

        # FAIL-CLOSED: If Redis session middleware failed to initialize, deny access
        if not self.redis_session_middleware_installed:
            logger.error(
                f"CRITICAL: Session middleware not available. Denying access to: {request.url.path}"
            )
            await self._send_error_response(
                scope,
                receive,
                send,
                request,
                error_type="session_unavailable",
                status_code=503,
                detail="Session management unavailable. Please contact administrator.",
            )
            return

        # FAIL-CLOSED: Check if APP_PASSWORD is configured
        if not settings.app_password:
            logger.error(
                "CRITICAL: APP_PASSWORD not configured but REQUIRE_APP_AUTH=true. Denying access."
            )
            await self._send_error_response(
                scope,
                receive,
                send,
                request,
                error_type="config",
                status_code=503,
                detail="Authentication not properly configured. Please contact administrator.",
            )
            return

        # FAIL-CLOSED: Validate session is accessible
        try:
            if not hasattr(request, "session"):
                raise AssertionError(
                    "SessionMiddleware not installed - request.session not available"
                )

            # Test session access
            _ = dict(request.session)

        except (AssertionError, AttributeError) as session_error:
            logger.error(
                f"CRITICAL: Session not accessible: {session_error}. Denying access to: {request.url.path}"
            )
            await self._send_error_response(
                scope,
                receive,
                send,
                request,
                error_type="session_failed",
                status_code=503,
                detail="Session system failed. Please contact administrator.",
            )
            return

        # Validate authentication
        try:
            is_valid = security_service.is_session_valid(request)

            if not is_valid:
                logger.debug("User not authenticated, denying access")
                await self._send_error_response(
                    scope,
                    receive,
                    send,
                    request,
                    error_type="auth_required",
                    status_code=401,
                    detail="Authentication required",
                    redirect_path=request.url.path,
                )
                return

            # User is authenticated, proceed with request
            await self.app(scope, receive, send)

        except HTTPException:
            # Re-raise HTTP exceptions (like 401, 302 redirects)
            raise
        except Exception as e:
            # FAIL-CLOSED: Any unexpected error denies access
            logger.error(f"Authentication error: {e}")
            logger.error(f"Request path: {request.url.path}")

            await self._send_error_response(
                scope,
                receive,
                send,
                request,
                error_type="auth_failed",
                status_code=503,
                detail="Authentication system error. Please contact administrator.",
            )

    async def _send_error_response(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
        request: Request,
        error_type: str,
        status_code: int,
        detail: str,
        redirect_path: str = None,
    ) -> None:
        """Send error response based on request type"""
        # For GET requests to HTML pages, redirect to login
        if request.method == "GET" and not request.url.path.startswith("/api"):
            # Build redirect URL
            if error_type == "auth_required" and redirect_path:
                redirect_url = f"/login?next={redirect_path}"
            else:
                redirect_url = f"/login?error={error_type}"

            # Create redirect response with cache prevention headers
            response = RedirectResponse(url=redirect_url, status_code=302)
            response.headers["Cache-Control"] = (
                "no-cache, no-store, must-revalidate, private"
            )
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

            await response(scope, receive, send)
        else:
            # For API requests, return JSON error
            response = Response(
                content=f'{{"detail": "{detail}"}}',
                status_code=status_code,
                media_type="application/json",
            )
            await response(scope, receive, send)


def create_redirect(url: str, status_code: int = 302) -> RedirectResponse:
    """Create a redirect response with cache prevention headers"""
    response = RedirectResponse(url=url, status_code=status_code)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response
