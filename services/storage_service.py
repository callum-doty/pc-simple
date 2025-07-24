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
            # For Render disks, the directory is guaranteed to exist.
            # For local dev, it should be created manually or with a setup script.
            if not os.path.exists(self.storage_path):
                if settings.environment == "development":
                    os.makedirs(self.storage_path)
                    logger.info(
                        f"Created development storage directory at: {self.storage_path}"
                    )
                else:
                    logger.warning(
                        f"Storage path {self.storage_path} not found. "
                        "This is expected on Render if the disk is attached."
                    )
            logger.info(f"Initialized local storage at: {self.storage_path}")
        except Exception as e:
            logger.error(f"Failed to initialize local storage: {str(e)}")
            raise

    def _init_s3_client(self):
        """Initialize S3 client"""
        try:
            endpoint_url = settings.s3_endpoint_url

            # If no endpoint is specified, construct it from the region for Backblaze
            if not endpoint_url and settings.s3_region:
                endpoint_url = f"https://s3.{settings.s3_region}.backblazeb2.com"
            elif endpoint_url and not endpoint_url.startswith("https://"):
                endpoint_url = f"https://{endpoint_url}"

            from botocore.client import Config

            self.s3_client = boto3.client(
                "s3",
                aws_access_key_id=settings.s3_access_key,
                aws_secret_access_key=settings.s3_secret_key,
                region_name=settings.s3_region,
                endpoint_url=endpoint_url,
                config=Config(signature_version="s3v4"),
            )

            # Test connection to the bucket
            try:
                self.s3_client.head_bucket(Bucket=settings.s3_bucket)
                logger.info(
                    f"Successfully initialized S3 storage for bucket: {settings.s3_bucket}"
                )
            except ClientError as e:
                logger.error(
                    f"Could not connect to S3 bucket '{settings.s3_bucket}'. "
                    f"Error: {e}. Please verify your S3 environment variables "
                    "(S3_BUCKET, S3_ACCESS_KEY, S3_SECRET_KEY, S3_REGION)."
                )
                # We log the error but don't raise, allowing the app to start.
                # File operations will likely fail until the configuration is corrected.

        except Exception as e:
            logger.error(
                f"An unexpected error occurred during S3 initialization: {str(e)}"
            )
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
        return filename

    async def _save_file_s3(self, file: UploadFile, filename: str) -> str:
        """Save file to S3 storage"""
        try:
            content = await file.read()
            await self.save_file_bytes(content, filename, file.content_type)
            logger.info(f"Saved file to S3: {filename}")
            return filename  # Return S3 key

        except Exception as e:
            logger.error(f"Error saving file to S3: {str(e)}")
            raise

    async def save_file_bytes(
        self, content: bytes, filename: str, content_type: Optional[str]
    ) -> None:
        """Save bytes to a file in storage."""
        if self.storage_type == "s3":
            self.s3_client.put_object(
                Bucket=settings.s3_bucket,
                Key=filename,
                Body=content,
                ContentType=content_type or "application/octet-stream",
            )
        else:
            file_path = Path(self.storage_path) / filename
            async with aiofiles.open(file_path, "wb") as f:
                await f.write(content)

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
        full_path = Path(self.storage_path) / Path(file_path).name
        try:
            async with aiofiles.open(full_path, "rb") as f:
                return await f.read()
        except FileNotFoundError:
            logger.warning(f"File not found: {full_path}")
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

    def _get_file_local_sync(self, file_path: str) -> Optional[bytes]:
        """Get file from local storage (synchronous)"""
        full_path = Path(self.storage_path) / Path(file_path).name
        try:
            with open(full_path, "rb") as f:
                return f.read()
        except FileNotFoundError:
            logger.warning(f"File not found: {full_path}")
            return None

    def _get_file_s3_sync(self, s3_key: str) -> Optional[bytes]:
        """Get file from S3 storage (synchronous)"""
        try:
            response = self.s3_client.get_object(Bucket=settings.s3_bucket, Key=s3_key)
            return response["Body"].read()
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                logger.warning(f"File not found in S3: {s3_key}")
            else:
                logger.error(f"S3 error getting file sync {s3_key}: {str(e)}")
            return None

    def get_file_sync(self, file_path: str) -> Optional[bytes]:
        """Get file content as bytes (synchronous)"""
        try:
            if self.storage_type == "s3":
                return self._get_file_s3_sync(file_path)
            else:
                return self._get_file_local_sync(file_path)
        except Exception as e:
            logger.error(f"Error getting file sync {file_path}: {str(e)}")
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
        preview_path = f"previews/{Path(file_path).stem}_preview.png"
        return await self.get_file_url(preview_path)

    def get_file_url_sync(
        self, file_path: str, expires_in: int = 3600
    ) -> Optional[str]:
        """Get URL for file access (synchronous)"""
        try:
            if self.storage_type == "s3":
                return self._get_s3_presigned_url(file_path, expires_in)
            else:
                return self._get_local_file_url(file_path)

        except Exception as e:
            logger.error(f"Error getting file URL {file_path}: {str(e)}")
            return None

    def get_preview_url_sync(self, file_path: str) -> Optional[str]:
        """Get preview URL for document (synchronous)"""
        preview_path = f"previews/{Path(file_path).stem}_preview.png"
        return self.get_file_url_sync(preview_path)

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
