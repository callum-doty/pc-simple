"""
Configuration settings for the simplified document catalog application
"""

from pydantic_settings import BaseSettings
from functools import lru_cache
import os


class Settings(BaseSettings):
    """Application settings"""

    # Basic app settings
    app_name: str = "Document Catalog"
    debug: bool = False
    secret_key: str = "your-secret-key-change-in-production"
    environment: str = "development"

    # Database settings
    database_url: str = "sqlite:///./documents.db"

    # Storage settings
    storage_type: str = "local"  # local, s3, render_disk
    storage_path: str = "./storage"
    max_file_size: int = 100 * 1024 * 1024  # 100MB
    allowed_extensions: list = [".pdf", ".jpg", ".jpeg", ".png", ".txt", ".docx"]

    # AI/LLM settings
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    default_ai_provider: str = "gemini"  # anthropic, openai, gemini

    # Search settings
    search_results_per_page: int = 20
    max_search_results: int = 1000

    # Processing settings
    max_concurrent_processing: int = 3
    processing_timeout: int = 300  # 5 minutes

    # Render-specific settings
    is_render: bool = False
    render_disk_path: str = "/opt/render/project/storage"

    # S3 settings (if using S3 storage)
    s3_bucket: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint_url: str = ""

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

        if (
            not self.anthropic_api_key
            and not self.openai_api_key
            and not self.gemini_api_key
        ):
            raise ValueError("At least one AI API key must be set in production")


class RenderSettings(ProductionSettings):
    """Render.com specific settings"""

    is_render: bool = True
    storage_type: str = "render_disk"
    storage_path: str = "/opt/render/project/storage"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

        # Use Render's provided DATABASE_URL if available
        render_db_url = os.getenv("DATABASE_URL")
        if render_db_url:
            self.database_url = render_db_url


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
