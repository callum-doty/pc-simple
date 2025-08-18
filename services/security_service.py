"""
Security service - handles authentication, authorization, and input validation
"""

import os
import logging
from pathlib import Path
from typing import Optional, List
from fastapi import HTTPException, Header, UploadFile, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import magic
import hashlib
import secrets
from datetime import datetime, timedelta

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

security = HTTPBearer(auto_error=False)


class SecurityService:
    """Security service for authentication and validation"""

    def __init__(self):
        self.api_key = settings.api_key
        self.require_auth = settings.require_auth
        self.max_file_size = settings.max_file_size_mb * 1024 * 1024  # Convert to bytes
        self.allowed_extensions = [
            ext.lower() for ext in settings.allowed_file_extensions
        ]

    def verify_api_key(self, authorization: Optional[str] = Header(None)) -> bool:
        """Verify API key authentication"""
        if not self.require_auth:
            return True

        if not authorization:
            raise HTTPException(status_code=401, detail="Authorization header required")

        if not authorization.startswith("Bearer "):
            raise HTTPException(
                status_code=401,
                detail="Invalid authorization header format. Use 'Bearer <api_key>'",
            )

        token = authorization.replace("Bearer ", "")

        if not self.api_key:
            logger.warning("API_KEY not configured but authentication is required")
            raise HTTPException(
                status_code=500, detail="Authentication not properly configured"
            )

        if token != self.api_key:
            raise HTTPException(status_code=401, detail="Invalid API key")

        return True

    def sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to prevent path traversal"""
        if not filename:
            raise HTTPException(status_code=400, detail="Filename cannot be empty")

        # Remove any path components
        filename = os.path.basename(filename)

        # Remove dangerous characters
        dangerous_chars = ["..", "/", "\\", ":", "*", "?", '"', "<", ">", "|"]
        for char in dangerous_chars:
            filename = filename.replace(char, "_")

        # Ensure filename is not empty after sanitization
        if not filename or filename.isspace():
            raise HTTPException(status_code=400, detail="Invalid filename")

        return filename

    def validate_file_path(self, file_path: str, base_path: str) -> str:
        """Validate file path to prevent directory traversal"""
        try:
            # Resolve the full path
            base_path = os.path.abspath(base_path)
            full_path = os.path.abspath(os.path.join(base_path, file_path))

            # Ensure the path is within the base directory
            if not full_path.startswith(base_path):
                raise HTTPException(
                    status_code=403, detail="Access denied: Path traversal detected"
                )

            return full_path

        except Exception as e:
            logger.error(f"Path validation error: {str(e)}")
            raise HTTPException(status_code=400, detail="Invalid file path")

    async def validate_upload_file(self, file: UploadFile) -> dict:
        """Comprehensive file upload validation"""
        validation_result = {
            "valid": True,
            "errors": [],
            "warnings": [],
            "file_info": {},
        }

        # Check if file exists
        if not file or not file.filename:
            validation_result["valid"] = False
            validation_result["errors"].append("No file provided")
            return validation_result

        # Sanitize filename
        try:
            sanitized_filename = self.sanitize_filename(file.filename)
            validation_result["file_info"]["sanitized_filename"] = sanitized_filename
        except HTTPException as e:
            validation_result["valid"] = False
            validation_result["errors"].append(e.detail)
            return validation_result

        # Check file extension
        file_extension = Path(file.filename).suffix.lower()
        if file_extension not in self.allowed_extensions:
            validation_result["valid"] = False
            validation_result["errors"].append(
                f"File type '{file_extension}' not allowed. "
                f"Allowed types: {', '.join(self.allowed_extensions)}"
            )

        # Check file size
        if file.size and file.size > self.max_file_size:
            validation_result["valid"] = False
            validation_result["errors"].append(
                f"File size ({file.size} bytes) exceeds maximum allowed size "
                f"({self.max_file_size} bytes)"
            )

        # Read file content for additional validation
        try:
            content = await file.read()
            await file.seek(0)  # Reset file pointer

            validation_result["file_info"]["actual_size"] = len(content)
            validation_result["file_info"]["content_hash"] = hashlib.sha256(
                content
            ).hexdigest()

            # Validate file content matches extension
            content_validation = self._validate_file_content(content, file_extension)
            if not content_validation["valid"]:
                validation_result["valid"] = False
                validation_result["errors"].extend(content_validation["errors"])
            else:
                validation_result["file_info"]["detected_type"] = content_validation[
                    "detected_type"
                ]

        except Exception as e:
            logger.error(f"Error reading file content: {str(e)}")
            validation_result["valid"] = False
            validation_result["errors"].append("Error reading file content")

        return validation_result

    def _validate_file_content(self, content: bytes, expected_extension: str) -> dict:
        """Validate file content matches expected type"""
        result = {"valid": True, "errors": [], "detected_type": None}

        try:
            # Use python-magic to detect actual file type
            detected_mime = magic.from_buffer(content, mime=True)
            result["detected_type"] = detected_mime

            # Define expected MIME types for extensions
            expected_mimes = {
                ".pdf": ["application/pdf"],
                ".jpg": ["image/jpeg"],
                ".jpeg": ["image/jpeg"],
                ".png": ["image/png"],
                ".txt": ["text/plain"],
                ".docx": [
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ],
            }

            if expected_extension in expected_mimes:
                if detected_mime not in expected_mimes[expected_extension]:
                    result["valid"] = False
                    result["errors"].append(
                        f"File content type '{detected_mime}' does not match "
                        f"extension '{expected_extension}'"
                    )

            # Check for potentially dangerous content
            dangerous_patterns = [
                b"<script",
                b"javascript:",
                b"vbscript:",
                b"onload=",
                b"onerror=",
                b"<?php",
                b"<%",
                b"#!/bin/sh",
                b"#!/bin/bash",
            ]

            content_lower = content.lower()
            for pattern in dangerous_patterns:
                if pattern in content_lower:
                    result["valid"] = False
                    result["errors"].append("Potentially malicious content detected")
                    break

        except Exception as e:
            logger.warning(f"Content validation error: {str(e)}")
            # Don't fail validation if magic detection fails
            pass

        return result

    def validate_search_query(self, query: str) -> str:
        """Validate and sanitize search query"""
        if not query:
            return ""

        # Remove potentially dangerous SQL injection patterns
        dangerous_patterns = [
            "';",
            "')",
            "';--",
            "' OR ",
            "' AND ",
            "UNION",
            "SELECT",
            "INSERT",
            "UPDATE",
            "DELETE",
            "DROP",
            "CREATE",
            "ALTER",
            "<script",
            "</script>",
            "javascript:",
            "vbscript:",
        ]

        query_upper = query.upper()
        for pattern in dangerous_patterns:
            if pattern.upper() in query_upper:
                raise HTTPException(
                    status_code=400, detail="Invalid characters in search query"
                )

        # Limit query length
        if len(query) > 500:
            raise HTTPException(
                status_code=400, detail="Search query too long (max 500 characters)"
            )

        return query.strip()

    def get_security_headers(self) -> dict:
        """Get security headers to add to responses"""
        # More permissive CSP for production compatibility
        csp_policy = (
            "default-src 'self' 'unsafe-inline' 'unsafe-eval' data: blob: https:; "
            "script-src 'self' 'unsafe-inline' 'unsafe-eval' https: data:; "
            "style-src 'self' 'unsafe-inline' https: data:; "
            "img-src 'self' data: blob: https: http:; "
            "font-src 'self' data: https:; "
            "connect-src 'self' https: wss: ws:; "
            "media-src 'self' data: blob: https:; "
            "object-src 'none'; "
            "base-uri 'self';"
        )

        return {
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "SAMEORIGIN",
            "X-XSS-Protection": "1; mode=block",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "Content-Security-Policy": csp_policy,
        }

    # Session management methods
    def verify_app_password(self, password: str) -> bool:
        """Verify the app-wide password"""
        if not settings.require_app_auth:
            return True

        return password == settings.app_password

    def create_session_token(self) -> str:
        """Create a secure session token"""
        return secrets.token_urlsafe(32)

    def is_session_valid(self, request: Request) -> bool:
        """Check if the current session is valid"""
        if not settings.require_app_auth:
            return True

        try:
            # Check if session middleware is available
            if not hasattr(request, "session") or "session" not in request.scope:
                logger.warning("SessionMiddleware not available - sessions disabled")
                return False

            session_token = request.session.get("auth_token")
            session_timestamp = request.session.get("auth_timestamp")

            if not session_token or not session_timestamp:
                return False

            # Check if session has expired
            try:
                auth_time = datetime.fromisoformat(session_timestamp)
                expiry_time = auth_time + timedelta(
                    hours=settings.session_timeout_hours
                )

                if datetime.now() > expiry_time:
                    return False

                return True
            except (ValueError, TypeError):
                return False
        except (AssertionError, AttributeError) as e:
            logger.error(f"Session validation error: {e}")
            return False

    def create_session(self, request: Request) -> str:
        """Create a new authenticated session"""
        try:
            # Check if session middleware is available
            if not hasattr(request, "session") or "session" not in request.scope:
                logger.error("SessionMiddleware not available - cannot create session")
                raise HTTPException(
                    status_code=500, detail="Session management not available"
                )

            session_token = self.create_session_token()
            request.session["auth_token"] = session_token
            request.session["auth_timestamp"] = datetime.now().isoformat()
            return session_token
        except (AssertionError, AttributeError) as e:
            logger.error(f"Session creation error: {e}")
            raise HTTPException(
                status_code=500, detail="Session management not available"
            )

    def destroy_session(self, request: Request):
        """Destroy the current session"""
        try:
            # Check if session middleware is available
            if not hasattr(request, "session") or "session" not in request.scope:
                logger.warning(
                    "SessionMiddleware not available - cannot destroy session"
                )
                return

            request.session.clear()
        except (AssertionError, AttributeError) as e:
            logger.error(f"Session destruction error: {e}")
            # Don't raise an exception for logout - just log the error

    def get_login_redirect_url(self, request: Request) -> str:
        """Get the URL to redirect to after login"""
        # Store the original URL they were trying to access
        return request.url.path if request.url.path != "/login" else "/"


# Global security service instance
security_service = SecurityService()
