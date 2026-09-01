"""
Configuration settings for the simplified document catalog application
"""

import logging
from pydantic_settings import BaseSettings
from functools import lru_cache
import os

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """Application settings"""

    # Basic app settings
    app_name: str = "Document Catalog"
    debug: bool = False
    secret_key: str = "your-secret-key-change-in-production"
    environment: str = "development"

    # Database settings
    database_url: str = "sqlite:///./documents.db"

    # Celery and Redis
    redis_url: str = "redis://localhost:6379/0"

    # Storage settings
    storage_type: str = "local"  # local, s3, render_disk
    storage_path: str = "./storage"
    max_file_size: int = 100 * 1024 * 1024  # 100MB
    allowed_extensions: list = [".pdf", ".jpg", ".jpeg", ".png", ".txt", ".docx"]

    # AI/LLM settings.
    # Anthropic performs all document analysis and OCR; OpenAI is embeddings-only.
    anthropic_api_key: str = ""
    openai_api_key: str = ""

    # Search settings
    search_results_per_page: int = 20
    max_search_results: int = 1000

    # Processing settings.
    # There is deliberately no app-level concurrency limit here. How many
    # documents process at once is set by the Celery worker's --concurrency
    # flag in render.yaml. The former max_concurrent_document_processing
    # setting was enforced by exactly one caller that never ran, so it read as
    # a live limit while the real one was somewhere else entirely.
    # Soft limit for one document-processing task. Celery raises
    # SoftTimeLimitExceeded inside the task at this point, so the existing
    # handler marks the document FAILED instead of leaving it PROCESSING.
    # Deliberately generous: nothing enforced this limit before, so some
    # documents currently succeed by running well past the old 300s value.
    # Tune down once processing_started_at has yielded a real p99.
    processing_timeout: int = 1800  # 30 minutes
    # Hard (SIGKILL) backstop for tasks that swallow the soft limit.
    processing_timeout_grace: int = 60
    # How far past the hard limit a heartbeat must be before the scheduler
    # calls a PROCESSING document a zombie.
    zombie_grace_seconds: int = 180
    # How far past zombie eligibility the Redis processing lease survives.
    lock_grace_seconds: int = 120
    # Replace a Celery child process after this many tasks, returning whatever
    # memory PDF rasterisation accumulated. Deliberately low: OCR holds page
    # bitmaps, and the container has been restarting roughly every five minutes,
    # so a child never survived long enough for a higher figure to take effect.
    # Env-tunable (WORKER_MAX_TASKS_PER_CHILD) so it can be adjusted from the
    # Render dashboard without a redeploy.
    worker_max_tasks_per_child: int = 10

    # Security settings
    api_key: str = ""
    require_auth: bool = False  # Default to False for development
    upload_password: str = "upload123"  # Simple password for uploads
    max_file_size_mb: int = 100
    allowed_file_extensions: list = [".pdf", ".jpg", ".jpeg", ".png", ".txt", ".docx"]

    # App-wide authentication settings
    app_password: str = (
        ""  # Password to access the entire app (set via APP_PASSWORD env var)
    )
    require_app_auth: bool = True  # Enable app-wide password protection
    session_timeout_hours: int = 24  # Session timeout in hours
    session_secret_key: str = (
        ""  # Secret key for session encryption (set via SESSION_SECRET_KEY env var)
    )

    # Render-specific settings
    is_render: bool = False
    render_disk_path: str = "/opt/render/project/storage"

    # S3 settings (if using S3 storage)
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""

    # CORS settings
    allowed_origins: str = ""  # Comma-separated list of allowed origins, e.g. "https://app.onrender.com"

    # Direct URL settings for performance optimization
    use_direct_urls: bool = True  # Use direct Backblaze URLs instead of proxy
    preview_url_expires_hours: int = 24  # Preview URLs expire after 24 hours
    download_url_expires_hours: int = 1  # Download URLs expire after 1 hour

    # Dropbox ingestion settings
    dropbox_app_key: str = ""
    dropbox_app_secret: str = ""
    dropbox_refresh_token: str = ""
    dropbox_folder_path: str = "/Press Files 2019-2020/2026"

    # ------------------------------------------------------------------
    # Derived processing timings. Kept here rather than in worker.py so the
    # worker and the recovery scheduler cannot drift apart. Required ordering:
    #
    #   soft limit < hard limit < zombie threshold < lease TTL < visibility
    #
    # Each step must be strictly greater than the one before it: a task is
    # killed before it can be called a zombie, called a zombie before its
    # lease expires, and its lease expires before the broker redelivers.
    # See docs/architecture-fixes/FIX-001.
    # ------------------------------------------------------------------

    @property
    def task_time_limit(self) -> int:
        """Hard task limit — Celery kills the worker process at this point."""
        return self.processing_timeout + self.processing_timeout_grace

    @property
    def zombie_threshold_seconds(self) -> int:
        """Heartbeat age past which a PROCESSING document is recoverable."""
        return self.task_time_limit + self.zombie_grace_seconds

    @property
    def lock_ttl_seconds(self) -> int:
        """TTL of the Redis processing lease."""
        return self.zombie_threshold_seconds + self.lock_grace_seconds

    @property
    def broker_visibility_timeout(self) -> int:
        """
        How long Redis waits before redelivering an un-acked message. With
        acks_late this must stay above the hard limit, or a still-running task
        is handed to a second worker.
        """
        return self.lock_ttl_seconds + 600

    def get_allowed_origins_list(self) -> list:
        """Parse the comma-separated ALLOWED_ORIGINS string into a list."""
        if not self.allowed_origins:
            return []
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    class Config:
        env_file = ".env"
        case_sensitive = False
        extra = "ignore"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Auto-detect Render environment
        if os.getenv("RENDER"):
            self.is_render = True
            if self.storage_type == "local":
                self.storage_type = "render_disk"
                self.storage_path = self.render_disk_path

        # Set debug mode based on environment
        if os.getenv("ENVIRONMENT") == "development":
            self.debug = True


class DevelopmentSettings(Settings):
    """Development-specific settings"""

    debug: bool = True
    database_url: str = "sqlite:///./dev_documents.db"
    storage_path: str = "./dev_storage"


class ProductionSettings(Settings):
    """Production-specific settings"""

    debug: bool = False

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Ensure required production settings
        if (
            not self.secret_key
            or self.secret_key == "your-secret-key-change-in-production"
        ):
            raise ValueError("SECRET_KEY must be set in production")

        if not self.anthropic_api_key:
            raise ValueError(
                "ANTHROPIC_API_KEY must be set in production (used for all document analysis and OCR)"
            )
        if not self.openai_api_key:
            raise ValueError(
                "OPENAI_API_KEY must be set in production (used for embeddings/vector search)"
            )

        # Ensure password protection is properly configured
        if self.require_app_auth:
            if not self.app_password:
                raise ValueError(
                    "APP_PASSWORD must be set when REQUIRE_APP_AUTH is enabled"
                )
            if not self.session_secret_key:
                raise ValueError(
                    "SESSION_SECRET_KEY must be set when REQUIRE_APP_AUTH is enabled"
                )


class RenderSettings(ProductionSettings):
    """Render.com specific settings"""

    is_render: bool = True

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Use Render's provided DATABASE_URL if available
        render_db_url = os.getenv("DATABASE_URL")
        if render_db_url:
            self.database_url = render_db_url

        # Use Render's provided REDIS_URL if available
        render_redis_url = os.getenv("REDIS_URL")
        if render_redis_url:
            self.redis_url = render_redis_url

        # Automatically configure storage for Render
        if self.s3_bucket and self.s3_access_key:
            self.storage_type = "s3"
            logger.info("Configured S3 storage for Render environment.")
        else:
            self.storage_type = "render_disk"
            self.storage_path = self.render_disk_path
            logger.info(
                "Configured Render disk storage. Note: Not suitable for multi-container setups."
            )


def validate_storage_config(settings: "Settings") -> None:
    """
    Validate storage configuration at startup.
    Raises RuntimeError if production is configured to use S3 but credentials are missing.
    Logs a WARNING for render_disk or local storage in production (allowed but discouraged).
    See docs/architecture-fixes/FIX-005.
    """
    env = getattr(settings, "environment", "development")
    if env not in ("production", "worker"):
        return  # skip in development

    storage_type = getattr(settings, "storage_type", "local")

    if storage_type == "s3":
        missing = []
        if not settings.s3_bucket:
            missing.append("S3_BUCKET")
        if not settings.s3_access_key:
            missing.append("S3_ACCESS_KEY")
        if not settings.s3_secret_key:
            missing.append("S3_SECRET_KEY")
        if not settings.s3_region and not settings.s3_endpoint_url:
            missing.append("S3_REGION or S3_ENDPOINT_URL")
        if missing:
            raise RuntimeError(
                f"STORAGE_TYPE=s3 but required S3 credentials are missing: "
                f"{', '.join(missing)}. Configure these in the Render environment "
                f"variables. Set STORAGE_TYPE=local to use local disk (not recommended "
                f"for production — files will be lost on service migration)."
            )
    elif storage_type == "render_disk":
        logger.warning(
            "STORAGE_TYPE=render_disk is set in production. This backend is tied to a "
            "single container — data will be lost on service migrations or scaling events. "
            "Switch to STORAGE_TYPE=s3 with Backblaze B2 for durable production storage."
        )
    elif storage_type == "local":
        logger.warning(
            "STORAGE_TYPE=local in production. Files are stored on the container's "
            "ephemeral filesystem and will not be accessible to other containers. "
            "Switch to STORAGE_TYPE=s3 for production deployments."
        )


@lru_cache()
def get_settings() -> Settings:
    """Get application settings (cached)"""
    environment = os.getenv("ENVIRONMENT", "development").lower()

    if environment == "production":
        if os.getenv("RENDER"):
            return RenderSettings()
        return ProductionSettings()
    elif environment == "development":
        return DevelopmentSettings()
    else:
        return Settings()


# Export commonly used settings
settings = get_settings()
