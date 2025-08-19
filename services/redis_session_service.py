"""
Redis-based session service - replaces SessionMiddleware with Redis storage
"""

import json
import logging
import secrets
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
import redis
from cryptography.fernet import Fernet
import base64
import hashlib

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class RedisSessionService:
    """Redis-based session management service"""

    def __init__(self):
        self.redis_client = None
        self.encryption_key = None
        self.session_prefix = "session:"
        self.default_ttl = settings.session_timeout_hours * 3600  # Convert to seconds
        self._initialize_redis()
        self._initialize_encryption()

    def _initialize_redis(self):
        """Initialize Redis connection"""
        try:
            # Parse Redis URL
            redis_url = settings.redis_url
            logger.info(f"Connecting to Redis at: {redis_url}")

            self.redis_client = redis.from_url(
                redis_url,
                decode_responses=True,
                socket_connect_timeout=5,
                socket_timeout=5,
                retry_on_timeout=True,
                health_check_interval=30,
            )

            # Test connection
            self.redis_client.ping()
            logger.info("Redis connection established successfully")

        except Exception as e:
            logger.error(f"Failed to connect to Redis: {e}")
            logger.warning("Session service will operate in fallback mode")
            self.redis_client = None

    def _initialize_encryption(self):
        """Initialize session data encryption"""
        try:
            # Use session secret key to derive encryption key
            session_secret = settings.session_secret_key
            if not session_secret:
                logger.warning("No session secret key configured, using temporary key")
                session_secret = secrets.token_urlsafe(32)

            # Derive a consistent encryption key from the session secret
            key_material = hashlib.sha256(session_secret.encode()).digest()
            self.encryption_key = base64.urlsafe_b64encode(key_material)

            # Test encryption
            fernet = Fernet(self.encryption_key)
            test_data = "test"
            encrypted = fernet.encrypt(test_data.encode())
            decrypted = fernet.decrypt(encrypted).decode()

            if decrypted != test_data:
                raise ValueError("Encryption test failed")

            logger.info("Session encryption initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize session encryption: {e}")
            self.encryption_key = None

    def _encrypt_data(self, data: str) -> str:
        """Encrypt session data"""
        if not self.encryption_key:
            return data

        try:
            fernet = Fernet(self.encryption_key)
            encrypted = fernet.encrypt(data.encode())
            return base64.urlsafe_b64encode(encrypted).decode()
        except Exception as e:
            logger.error(f"Failed to encrypt session data: {e}")
            return data

    def _decrypt_data(self, encrypted_data: str) -> str:
        """Decrypt session data"""
        if not self.encryption_key:
            return encrypted_data

        try:
            fernet = Fernet(self.encryption_key)
            decoded = base64.urlsafe_b64decode(encrypted_data.encode())
            decrypted = fernet.decrypt(decoded)
            return decrypted.decode()
        except Exception as e:
            logger.error(f"Failed to decrypt session data: {e}")
            return encrypted_data

    def generate_session_id(self) -> str:
        """Generate a secure session ID"""
        return secrets.token_urlsafe(32)

    def _get_session_key(self, session_id: str) -> str:
        """Get Redis key for session"""
        return f"{self.session_prefix}{session_id}"

    def create_session(self, session_data: Dict[str, Any] = None) -> str:
        """Create a new session and return session ID"""
        if not self.redis_client:
            logger.warning("Redis not available, cannot create persistent session")
            return None

        try:
            session_id = self.generate_session_id()
            session_key = self._get_session_key(session_id)

            # Initialize session data
            if session_data is None:
                session_data = {}

            # Add metadata
            session_data["_created_at"] = datetime.now().isoformat()
            session_data["_last_accessed"] = datetime.now().isoformat()

            # Serialize and encrypt
            serialized_data = json.dumps(session_data)
            encrypted_data = self._encrypt_data(serialized_data)

            # Store in Redis with TTL
            self.redis_client.setex(session_key, self.default_ttl, encrypted_data)

            logger.debug(f"Created session {session_id}")
            return session_id

        except Exception as e:
            logger.error(f"Failed to create session: {e}")
            return None

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve session data by session ID"""
        if not self.redis_client or not session_id:
            return None

        try:
            session_key = self._get_session_key(session_id)
            encrypted_data = self.redis_client.get(session_key)

            if not encrypted_data:
                logger.debug(f"Session {session_id} not found or expired")
                return None

            # Decrypt and deserialize
            decrypted_data = self._decrypt_data(encrypted_data)
            session_data = json.loads(decrypted_data)

            # Update last accessed time
            session_data["_last_accessed"] = datetime.now().isoformat()
            self.update_session(session_id, session_data)

            logger.debug(f"Retrieved session {session_id}")
            return session_data

        except Exception as e:
            logger.error(f"Failed to retrieve session {session_id}: {e}")
            return None

    def update_session(self, session_id: str, session_data: Dict[str, Any]) -> bool:
        """Update session data"""
        if not self.redis_client or not session_id:
            return False

        try:
            session_key = self._get_session_key(session_id)

            # Check if session exists
            if not self.redis_client.exists(session_key):
                logger.debug(f"Session {session_id} does not exist, cannot update")
                return False

            # Update last accessed time
            session_data["_last_accessed"] = datetime.now().isoformat()

            # Serialize and encrypt
            serialized_data = json.dumps(session_data)
            encrypted_data = self._encrypt_data(serialized_data)

            # Update in Redis, preserving TTL
            ttl = self.redis_client.ttl(session_key)
            if ttl > 0:
                self.redis_client.setex(session_key, ttl, encrypted_data)
            else:
                # If no TTL, set default
                self.redis_client.setex(session_key, self.default_ttl, encrypted_data)

            logger.debug(f"Updated session {session_id}")
            return True

        except Exception as e:
            logger.error(f"Failed to update session {session_id}: {e}")
            return False

    def delete_session(self, session_id: str) -> bool:
        """Delete a session"""
        if not self.redis_client or not session_id:
            return False

        try:
            session_key = self._get_session_key(session_id)
            result = self.redis_client.delete(session_key)

            logger.debug(f"Deleted session {session_id}")
            return result > 0

        except Exception as e:
            logger.error(f"Failed to delete session {session_id}: {e}")
            return False

    def extend_session(self, session_id: str, ttl_seconds: int = None) -> bool:
        """Extend session TTL"""
        if not self.redis_client or not session_id:
            return False

        try:
            session_key = self._get_session_key(session_id)

            if not self.redis_client.exists(session_key):
                return False

            if ttl_seconds is None:
                ttl_seconds = self.default_ttl

            result = self.redis_client.expire(session_key, ttl_seconds)
            logger.debug(f"Extended session {session_id} TTL to {ttl_seconds} seconds")
            return result

        except Exception as e:
            logger.error(f"Failed to extend session {session_id}: {e}")
            return False

    def get_session_ttl(self, session_id: str) -> Optional[int]:
        """Get remaining TTL for a session"""
        if not self.redis_client or not session_id:
            return None

        try:
            session_key = self._get_session_key(session_id)
            ttl = self.redis_client.ttl(session_key)
            return ttl if ttl > 0 else None

        except Exception as e:
            logger.error(f"Failed to get TTL for session {session_id}: {e}")
            return None

    def cleanup_expired_sessions(self) -> int:
        """Clean up expired sessions (Redis handles this automatically, but useful for stats)"""
        if not self.redis_client:
            return 0

        try:
            # Get all session keys
            pattern = f"{self.session_prefix}*"
            keys = self.redis_client.keys(pattern)

            expired_count = 0
            for key in keys:
                ttl = self.redis_client.ttl(key)
                if ttl == -2:  # Key doesn't exist (expired)
                    expired_count += 1

            logger.info(f"Found {expired_count} expired sessions")
            return expired_count

        except Exception as e:
            logger.error(f"Failed to cleanup expired sessions: {e}")
            return 0

    def get_session_stats(self) -> Dict[str, Any]:
        """Get session statistics"""
        if not self.redis_client:
            return {"error": "Redis not available"}

        try:
            pattern = f"{self.session_prefix}*"
            keys = self.redis_client.keys(pattern)

            total_sessions = len(keys)
            active_sessions = 0

            for key in keys:
                ttl = self.redis_client.ttl(key)
                if ttl > 0:
                    active_sessions += 1

            return {
                "total_sessions": total_sessions,
                "active_sessions": active_sessions,
                "redis_connected": True,
                "default_ttl_hours": self.default_ttl / 3600,
            }

        except Exception as e:
            logger.error(f"Failed to get session stats: {e}")
            return {"error": str(e)}

    def health_check(self) -> Dict[str, Any]:
        """Health check for Redis session service"""
        try:
            if not self.redis_client:
                return {"status": "unhealthy", "error": "Redis client not initialized"}

            # Test Redis connection
            self.redis_client.ping()

            # Test session operations
            test_session_id = self.create_session({"test": "data"})
            if test_session_id:
                session_data = self.get_session(test_session_id)
                self.delete_session(test_session_id)

                if session_data and session_data.get("test") == "data":
                    return {
                        "status": "healthy",
                        "redis_connected": True,
                        "encryption_enabled": bool(self.encryption_key),
                    }

            return {"status": "unhealthy", "error": "Session operations test failed"}

        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}


# Global session service instance
redis_session_service = RedisSessionService()
