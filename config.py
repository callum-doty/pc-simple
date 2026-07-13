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

    # Processing settings
    max_concurrent_processing: int = 3
    max_concurrent_document_processing: int = 3
    processing_timeout: int = 300  # 5 minutes

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
