"""
Preview service - generates image previews from documents and stores them in the configured storage backend.
"""

import logging
from pathlib import Path
from typing import Optional
import fitz  # PyMuPDF
from PIL import Image
import io

from config import get_settings
from services.storage_service import StorageService

logger = logging.getLogger(__name__)
settings = get_settings()


class PreviewService:
    """Service for generating and managing document previews"""

    def __init__(self, storage_service: StorageService):
        self.storage = storage_service
        self.preview_prefix = "previews"

    async def _generate_pdf_preview_bytes(self, pdf_content: bytes) -> Optional[bytes]:
        """Generate a preview image from PDF content and return as bytes"""
        try:
            doc = fitz.open(stream=pdf_content, filetype="pdf")
            page = doc[0]
            mat = fitz.Matrix(2.0, 2.0)
            pix = page.get_pixmap(matrix=mat)
            img_data = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_data))
            img.thumbnail((300, 400), Image.Resampling.LANCZOS)

            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format="PNG", optimize=True)
            doc.close()
            return img_byte_arr.getvalue()
        except Exception as e:
            logger.error(f"Error generating PDF preview bytes: {str(e)}")
            return None

    async def _generate_image_preview_bytes(
        self, image_content: bytes
    ) -> Optional[bytes]:
        """Generate a preview from image content and return as bytes"""
        try:
            img = Image.open(io.BytesIO(image_content))
            if img.mode in ("RGBA", "LA", "P"):
                img = img.convert("RGB")
            img.thumbnail((300, 400), Image.Resampling.LANCZOS)

            img_byte_arr = io.BytesIO()
            img.save(img_byte_arr, format="PNG", optimize=True)
            return img_byte_arr.getvalue()
        except Exception as e:
            logger.error(f"Error generating image preview bytes: {str(e)}")
            return None

    def get_preview_path(self, original_file_path: str) -> str:
        """Get the preview file path for a given document in storage"""
        file_name = Path(original_file_path).stem
        preview_filename = f"{file_name}_preview.png"
        return f"{self.preview_prefix}/{preview_filename}"

    async def generate_preview(self, original_file_path: str) -> Optional[str]:
        """
        Generate a preview for a file in storage and save it back to storage.
        Returns the path of the preview file in storage.
        """
        preview_path = self.get_preview_path(original_file_path)

        # Check if preview already exists in storage
        if await self.storage.check_file_exists(preview_path):
            return preview_path

        # Get the original file content from storage
        file_content = await self.storage.get_file(original_file_path)
        if not file_content:
            logger.warning(
                f"Could not retrieve file from storage: {original_file_path}"
            )
            return None

        file_ext = Path(original_file_path).suffix.lower()
        preview_bytes = None

        if file_ext == ".pdf":
            preview_bytes = await self._generate_pdf_preview_bytes(file_content)
        elif file_ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"]:
            preview_bytes = await self._generate_image_preview_bytes(file_content)
        else:
            logger.warning(f"Unsupported file type for preview: {file_ext}")
            return None

        if preview_bytes:
            # Save the preview to storage
            await self.storage.save_file_bytes(preview_bytes, preview_path, "image/png")
            return preview_path

        return None

    def generate_preview_sync(self, original_file_path: str) -> Optional[str]:
        """
        Synchronous version of generate_preview for use in Celery tasks.
        """
        preview_path = self.get_preview_path(original_file_path)

        # Synchronous check if preview exists
        # This assumes storage service has a synchronous check_file_exists method
        # For this implementation, we'll proceed and overwrite for simplicity,
        # assuming the check is not critical for the sync worker.

        file_content = self.storage.get_file_sync(original_file_path)
        if not file_content:
            logger.warning(
                f"Could not retrieve file from storage (sync): {original_file_path}"
            )
            return None

        file_ext = Path(original_file_path).suffix.lower()
        preview_bytes = None

        if file_ext == ".pdf":
            # This part needs to be synchronous
            try:
                doc = fitz.open(stream=file_content, filetype="pdf")
                page = doc[0]
                mat = fitz.Matrix(2.0, 2.0)
                pix = page.get_pixmap(matrix=mat)
                img_data = pix.tobytes("png")
                img = Image.open(io.BytesIO(img_data))
                img.thumbnail((300, 400), Image.Resampling.LANCZOS)
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format="PNG", optimize=True)
                doc.close()
                preview_bytes = img_byte_arr.getvalue()
            except Exception as e:
                logger.error(f"Error generating PDF preview bytes (sync): {str(e)}")
                return None
        elif file_ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"]:
            # This part needs to be synchronous
            try:
                img = Image.open(io.BytesIO(file_content))
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGB")
                img.thumbnail((300, 400), Image.Resampling.LANCZOS)
                img_byte_arr = io.BytesIO()
                img.save(img_byte_arr, format="PNG", optimize=True)
                preview_bytes = img_byte_arr.getvalue()
            except Exception as e:
                logger.error(f"Error generating image preview bytes (sync): {str(e)}")
                return None
        else:
            logger.warning(f"Unsupported file type for preview (sync): {file_ext}")
            return None

        if preview_bytes:
            # Synchronous save
            # This assumes storage service has a synchronous save_file_bytes method
            self.storage.save_file_bytes_sync(preview_bytes, preview_path, "image/png")
            return preview_path

        return None

    async def get_preview_url(
        self, original_file_path: str, expires_in: int = 3600
    ) -> Optional[str]:
        """
        Get a URL for a document preview. Generates the preview if it doesn't exist.
        """
        preview_path = await self.generate_preview(original_file_path)
        if preview_path:
            return await self.storage.get_file_url(
                preview_path, expires_in, content_type="image/png"
            )
        return None

    async def delete_preview(self, original_file_path: str) -> bool:
        """Delete a preview file from storage."""
        preview_path = self.get_preview_path(original_file_path)
        return await self.storage.delete_file(preview_path)
