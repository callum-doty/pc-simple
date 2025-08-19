"""
Redis-based session middleware - replaces Starlette's SessionMiddleware
"""

import logging
from typing import Dict, Any, Optional
from starlette.types import ASGIApp, Receive, Scope, Send
from starlette.requests import Request
from starlette.responses import Response
import secrets
import time

from services.redis_session_service import redis_session_service

logger = logging.getLogger(__name__)


class RedisSession(dict):
    """Session object that behaves like a dictionary but persists to Redis"""

    def __init__(self, session_id: str = None, initial_data: Dict[str, Any] = None):
        super().__init__()
        self._session_id = session_id
        self._modified = False
        self._new = session_id is None

        # Load initial data
        if initial_data:
            self.update(initial_data)
            self._modified = False  # Reset modified flag after initial load

    @property
    def session_id(self) -> Optional[str]:
        """Get the session ID"""
        return self._session_id

    @property
    def is_new(self) -> bool:
        """Check if this is a new session"""
        return self._new

    @property
    def is_modified(self) -> bool:
        """Check if the session has been modified"""
        return self._modified

    def __setitem__(self, key, value):
        """Override to track modifications"""
        super().__setitem__(key, value)
        self._modified = True

    def __delitem__(self, key):
        """Override to track modifications"""
        super().__delitem__(key)
        self._modified = True

    def clear(self):
        """Override to track modifications"""
        super().clear()
        self._modified = True

    def pop(self, key, default=None):
        """Override to track modifications"""
        self._modified = True
        return super().pop(key, default)

    def popitem(self):
        """Override to track modifications"""
        self._modified = True
        return super().popitem()

    def setdefault(self, key, default=None):
        """Override to track modifications"""
        if key not in self:
            self._modified = True
        return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        """Override to track modifications"""
        if args or kwargs:
            self._modified = True
        super().update(*args, **kwargs)

    def save(self) -> bool:
        """Save session to Redis"""
        if not self._modified and not self._new:
            return True  # No changes to save

        try:
            if self._new:
                # Create new session
                self._session_id = redis_session_service.create_session(dict(self))
                if self._session_id:
                    self._new = False
                    self._modified = False
                    logger.debug(f"Created new session: {self._session_id}")
                    return True
                else:
                    logger.error("Failed to create new session")
                    return False
            else:
                # Update existing session
                success = redis_session_service.update_session(
                    self._session_id, dict(self)
                )
                if success:
                    self._modified = False
                    logger.debug(f"Updated session: {self._session_id}")
                    return True
                else:
                    logger.error(f"Failed to update session: {self._session_id}")
                    return False

        except Exception as e:
            logger.error(f"Error saving session: {e}")
            return False

    def delete(self) -> bool:
        """Delete session from Redis"""
        if not self._session_id:
            return True

        try:
            success = redis_session_service.delete_session(self._session_id)
            if success:
                self.clear()
                self._session_id = None
                self._new = True
                self._modified = False
                logger.debug("Session deleted successfully")
            return success
        except Exception as e:
            logger.error(f"Error deleting session: {e}")
            return False


class RedisSessionMiddleware:
    """Redis-based session middleware"""

    def __init__(
        self,
        app: ASGIApp,
        secret_key: str,
        session_cookie: str = "session",
        max_age: int = 14 * 24 * 60 * 60,  # 14 days
        path: str = "/",
        same_site: str = "lax",
        https_only: bool = False,
        domain: str = None,
    ):
        self.app = app
        self.secret_key = secret_key
        self.session_cookie = session_cookie
        self.max_age = max_age
        self.path = path
        self.same_site = same_site
        self.https_only = https_only
        self.domain = domain

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Load session
        session = await self._load_session(scope)
        scope["session"] = session

        # Wrap send to save session on response
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                # Save session before sending response
                await self._save_session(session, message)
            await send(message)

        await self.app(scope, receive, send_wrapper)

    async def _load_session(self, scope: Scope) -> RedisSession:
        """Load session from Redis"""
        try:
            # Get session ID from cookie
            session_id = None
            cookies = {}

            # Parse cookies from headers
            for name, value in scope.get("headers", []):
                if name == b"cookie":
                    cookie_header = value.decode("latin1")
                    for cookie in cookie_header.split(";"):
                        if "=" in cookie:
                            key, val = cookie.strip().split("=", 1)
                            cookies[key] = val

            session_id = cookies.get(self.session_cookie)

            if session_id:
                # Try to load existing session
                session_data = redis_session_service.get_session(session_id)
                if session_data:
                    logger.debug(f"Loaded existing session: {session_id}")
                    return RedisSession(session_id, session_data)
                else:
                    logger.debug(f"Session {session_id} not found or expired")

            # Create new session
            logger.debug("Creating new session")
            return RedisSession()

        except Exception as e:
            logger.error(f"Error loading session: {e}")
            return RedisSession()

    async def _save_session(self, session: RedisSession, message: dict) -> None:
        """Save session and set cookie"""
        try:
            # Save session to Redis
            if session.is_modified or session.is_new:
                success = session.save()
                if not success:
                    logger.error("Failed to save session")
                    return

            # Set session cookie if we have a session ID
            if session.session_id:
                cookie_value = self._create_session_cookie(session.session_id)

                # Add Set-Cookie header
                headers = list(message.get("headers", []))
                headers.append((b"set-cookie", cookie_value.encode("latin1")))
                message["headers"] = headers

        except Exception as e:
            logger.error(f"Error saving session: {e}")

    def _create_session_cookie(self, session_id: str) -> str:
        """Create session cookie string"""
        cookie_parts = [f"{self.session_cookie}={session_id}"]

        if self.max_age:
            cookie_parts.append(f"Max-Age={self.max_age}")

        if self.path:
            cookie_parts.append(f"Path={self.path}")

        if self.domain:
            cookie_parts.append(f"Domain={self.domain}")

        if self.same_site:
            cookie_parts.append(f"SameSite={self.same_site}")

        if self.https_only:
            cookie_parts.append("Secure")

        cookie_parts.append("HttpOnly")

        return "; ".join(cookie_parts)


class FallbackSession(dict):
    """Fallback session that works in memory when Redis is unavailable"""

    def __init__(self):
        super().__init__()
        self._modified = False

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._modified = True

    def __delitem__(self, key):
        super().__delitem__(key)
        self._modified = True

    def clear(self):
        super().clear()
        self._modified = True

    def pop(self, key, default=None):
        self._modified = True
        return super().pop(key, default)

    def popitem(self):
        self._modified = True
        return super().popitem()

    def setdefault(self, key, default=None):
        if key not in self:
            self._modified = True
        return super().setdefault(key, default)

    def update(self, *args, **kwargs):
        if args or kwargs:
            self._modified = True
        super().update(*args, **kwargs)


class FallbackSessionMiddleware:
    """Fallback session middleware when Redis is not available"""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        # Create a fallback session that doesn't persist
        scope["session"] = FallbackSession()

        # Add warning header
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append(
                    (b"x-session-warning", b"Fallback session - data will not persist")
                )
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)
