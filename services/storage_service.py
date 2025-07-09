"""
Storage service - handles file storage operations
Supports local storage, Render disk storage, and S3-compatible storage
"""

import os
import shutil
import uuid
from pathlib import Path
from typing import Optional, BinaryIO
import logging
from fastapi import UploadFile
import aiofiles
import boto3
from botocore.exceptions import ClientError

from config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


class StorageService:
    """Unified storage service supporting multiple backends"""

    def __init__(self):
        self.storage_type = settings.storage_type
        self.storage_path = settings.storage_path

        # Initialize storage backend
        if self.storage_type == "s3":
            self._init_s3_client()
        else:
            self._init_local_storage()

    def _init_local_storage(self):
        """Initialize local/render disk storage"""
        try:
            # Create storage directory if it doesn't exist
            Path(self.storage_path).mkdir(parents=True, exist_ok=True)
            logger.info(f"Initialized local storage at: {self.storage_path}")
        except Exception as e:
            logger.error(f"Failed to initialize local storage: {str(e)}")
            raise

    def _init_s3_client(self):
        """Initialize S3 client"""
        try:
            self.s3_client = boto3.client(
                "s3",
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
                endpoint_url=(
                    settings.s3_endpoint_url if settings.s3_endpoint_url else None
                ),
            )

            # Test connection
            self.s3_client.head_bucket(Bucket=settings.s3_bucket)
            logger.info(f"Initialized S3 storage with bucket: {settings.s3_bucket}")

        except Exception as e:
            logger.error(f"Failed to initialize S3 storage: {str(e)}")
            raise

    async def save_file(self, file: UploadFile) -> str:
        """Save uploaded file and return file path"""
        try:
            # Generate unique filename
            file_extension = Path(file.filename).suffix
            unique_filename = f"{uuid.uuid4()}{file_extension}"

            if self.storage_type == "s3":
                return await self._save_file_s3(file, unique_filename)
            else:
                return await self._save_file_local(file, unique_filename)

        except Exception as e:
            logger.error(f"Error saving file {file.filename}: {str(e)}")
            raise

    async def _save_file_local(self, file: UploadFile, filename: str) -> str:
        """Save file to local storage"""
        file_path = Path(self.storage_path) / filename

        async with aiofiles.open(file_path, "wb") as f:
            content = await file.read()
            await f.write(content)

        logger.info(f"Saved file locally: {filename}")
        return str(file_path)

    async def _save_file_s3(self, file: UploadFile, filename: str) -> str:
        """Save file to S3 storage"""
        try:
            content = await file.read()

            self.s3_client.put_object(
                Bucket=settings.s3_bucket,
                Key=filename,
                Body=content,
                ContentType=file.content_type or "application/octet-stream",
            )

            logger.info(f"Saved file to S3: {filename}")
            return filename  # Return S3 key

        except Exception as e:
            logger.error(f"Error saving file to S3: {str(e)}")
            raise

    async def get_file(self, file_path: str) -> Optional[bytes]:
        """Get file content as bytes"""
        try:
            if self.storage_type == "s3":
                return await self._get_file_s3(file_path)
            else:
                return await self._get_file_local(file_path)

        except Exception as e:
            logger.error(f"Error getting file {file_path}: {str(e)}")
            return None

    async def _get_file_local(self, file_path: str) -> Optional[bytes]:
        """Get file from local storage"""
        try:
            async with aiofiles.open(file_path, "rb") as f:
                return await f.read()
        except FileNotFoundError:
            logger.warning(f"File not found: {file_path}")
            return None

    async def _get_file_s3(self, s3_key: str) -> Optional[bytes]:
        """Get file from S3 storage"""
        try:
            response = self.s3_client.get_object(Bucket=settings.s3_bucket, Key=s3_key)
            return response["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                logger.warning(f"File not found in S3: {s3_key}")
            else:
                logger.error(f"S3 error getting file {s3_key}: {str(e)}")
            return None

    async def delete_file(self, file_path: str) -> bool:
        """Delete file from storage"""
        try:
            if self.storage_type == "s3":
                return await self._delete_file_s3(file_path)
            else:
                return await self._delete_file_local(file_path)

        except Exception as e:
            logger.error(f"Error deleting file {file_path}: {str(e)}")
            return False

    async def _delete_file_local(self, file_path: str) -> bool:
        """Delete file from local storage"""
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.info(f"Deleted local file: {file_path}")
                return True
            return False
        except Exception as e:
            logger.error(f"Error deleting local file {file_path}: {str(e)}")
            return False

    async def _delete_file_s3(self, s3_key: str) -> bool:
        """Delete file from S3 storage"""
        try:
            self.s3_client.delete_object(Bucket=settings.s3_bucket, Key=s3_key)
            logger.info(f"Deleted S3 file: {s3_key}")
            return True
        except Exception as e:
            logger.error(f"Error deleting S3 file {s3_key}: {str(e)}")
            return False

    async def get_file_url(
        self, file_path: str, expires_in: int = 3600
    ) -> Optional[str]:
        """Get URL for file access"""
        try:
            if self.storage_type == "s3":
                return self._get_s3_presigned_url(file_path, expires_in)
            else:
                return self._get_local_file_url(file_path)

        except Exception as e:
            logger.error(f"Error getting file URL {file_path}: {str(e)}")
            return None

    def _get_local_file_url(self, file_path: str) -> str:
        """Get URL for local file (for development)"""
        # For local development, return a path that can be served by FastAPI
        filename = Path(file_path).name
        return f"/files/{filename}"

    def _get_s3_presigned_url(self, s3_key: str, expires_in: int) -> Optional[str]:
        """Get presigned URL for S3 file"""
        try:
            url = self.s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": settings.s3_bucket, "Key": s3_key},
                ExpiresIn=expires_in,
            )
            return url
        except Exception as e:
            logger.error(f"Error generating presigned URL for {s3_key}: {str(e)}")
            return None

    async def get_preview_url(self, file_path: str) -> Optional[str]:
        """Get preview URL for document"""
        # For now, return the same as file URL
        # In a more complex implementation, this could generate thumbnails
        return await self.get_file_url(file_path)

    def get_storage_info(self) -> dict:
        """Get storage configuration info"""
        return {
            "storage_type": self.storage_type,
            "storage_path": self.storage_path if self.storage_type != "s3" else None,
            "s3_bucket": settings.s3_bucket if self.storage_type == "s3" else None,
            "s3_region": settings.s3_region if self.storage_type == "s3" else None,
        }

    async def check_file_exists(self, file_path: str) -> bool:
        """Check if file exists in storage"""
        try:
            if self.storage_type == "s3":
                try:
                    self.s3_client.head_object(Bucket=settings.s3_bucket, Key=file_path)
                    return True
                except ClientError:
                    return False
            else:
                return os.path.exists(file_path)

        except Exception as e:
            logger.error(f"Error checking file existence {file_path}: {str(e)}")
            return False

    async def get_file_size(self, file_path: str) -> Optional[int]:
        """Get file size in bytes"""
        try:
            if self.storage_type == "s3":
                try:
                    response = self.s3_client.head_object(
                        Bucket=settings.s3_bucket, Key=file_path
                    )
                    return response["ContentLength"]
                except ClientError:
                    return None
            else:
                if os.path.exists(file_path):
                    return os.path.getsize(file_path)
                return None

        except Exception as e:
            logger.error(f"Error getting file size {file_path}: {str(e)}")
            return None
