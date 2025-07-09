"""
Preview service - generates image previews from documents
"""

import os
import logging
from pathlib import Path
from typing import Optional
import fitz  # PyMuPDF
from PIL import Image
import io

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class PreviewService:
    """Service for generating document previews"""

    def __init__(self):
        self.preview_dir = Path(settings.storage_path) / "previews"
        self.preview_dir.mkdir(exist_ok=True)

    def generate_pdf_preview(self, pdf_path: str, output_path: str) -> bool:
        """Generate a preview image from a PDF file"""
        try:
            # Open the PDF
            doc = fitz.open(pdf_path)

            # Get the first page
            page = doc[0]

            # Render page to a pixmap (image)
            mat = fitz.Matrix(2.0, 2.0)  # 2x zoom for better quality
            pix = page.get_pixmap(matrix=mat)

            # Convert to PIL Image
            img_data = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_data))

            # Resize to thumbnail size (max 300x400, maintain aspect ratio)
            img.thumbnail((300, 400), Image.Resampling.LANCZOS)

            # Save as PNG
            img.save(output_path, "PNG", optimize=True)

            doc.close()
            logger.info(f"Generated preview for PDF: {pdf_path}")
            return True

        except Exception as e:
            logger.error(f"Error generating PDF preview for {pdf_path}: {str(e)}")
            return False

    def generate_image_preview(self, image_path: str, output_path: str) -> bool:
        """Generate a preview from an image file"""
        try:
            with Image.open(image_path) as img:
                # Convert to RGB if necessary
                if img.mode in ("RGBA", "LA", "P"):
                    img = img.convert("RGB")

                # Resize to thumbnail size
                img.thumbnail((300, 400), Image.Resampling.LANCZOS)

                # Save as PNG
                img.save(output_path, "PNG", optimize=True)

            logger.info(f"Generated preview for image: {image_path}")
            return True

        except Exception as e:
            logger.error(f"Error generating image preview for {image_path}: {str(e)}")
            return False

    def get_preview_path(self, file_path: str) -> str:
        """Get the preview file path for a given document"""
        file_name = Path(file_path).stem
        preview_filename = f"{file_name}_preview.png"
        return str(self.preview_dir / preview_filename)

    def generate_preview(self, file_path: str) -> Optional[str]:
        """Generate a preview for any supported file type"""
        if not os.path.exists(file_path):
            logger.warning(f"File not found: {file_path}")
            return None

        preview_path = self.get_preview_path(file_path)

        # Check if preview already exists
        if os.path.exists(preview_path):
            return preview_path

        # Determine file type and generate preview
        file_ext = Path(file_path).suffix.lower()

        success = False
        if file_ext == ".pdf":
            success = self.generate_pdf_preview(file_path, preview_path)
        elif file_ext in [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff"]:
            success = self.generate_image_preview(file_path, preview_path)
        else:
            logger.warning(f"Unsupported file type for preview: {file_ext}")
            return None

        if success and os.path.exists(preview_path):
            return preview_path
        else:
            return None

    def get_preview_url(self, file_path: str) -> Optional[str]:
        """Get the URL for a document preview"""
        preview_path = self.generate_preview(file_path)

        if preview_path:
            # Convert absolute path to relative URL
            preview_filename = Path(preview_path).name
            return f"/previews/{preview_filename}"

        return None

    def cleanup_old_previews(self, max_age_days: int = 30):
        """Clean up old preview files"""
        try:
            import time

            current_time = time.time()
            max_age_seconds = max_age_days * 24 * 60 * 60

            for preview_file in self.preview_dir.glob("*.png"):
                file_age = current_time - preview_file.stat().st_mtime
                if file_age > max_age_seconds:
                    preview_file.unlink()
                    logger.info(f"Deleted old preview: {preview_file}")

        except Exception as e:
            logger.error(f"Error cleaning up previews: {str(e)}")
