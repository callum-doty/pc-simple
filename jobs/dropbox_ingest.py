"""
Dropbox ingestion job — run as a Render cron job.

Flow:
  1. Load cursor from DB (null on first run → full folder scan)
  2. Ask Dropbox for new/changed files since cursor
  3. For each file: skip if dropbox_file_id OR content_hash already exists
  4. Download → store → create document record (QUEUED) → Celery picks it up
  5. Persist the new cursor so the next run is incremental
"""

import hashlib
import logging
import mimetypes
import sys
import uuid
from pathlib import Path

from sqlalchemy import text

from database import SessionLocal
from models.document import Document, DocumentStatus
from services.dropbox_service import DropboxService
from services.storage_service import StorageService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [dropbox_ingest] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

ALLOWED_EXTENSIONS = {".pdf"}


def _get_cursor(db) -> str | None:
    row = db.execute(text("SELECT cursor FROM dropbox_sync_state WHERE id = 1")).fetchone()
    return row[0] if row else None


def _save_cursor(db, cursor: str) -> None:
    db.execute(
        text("UPDATE dropbox_sync_state SET cursor = :cursor, updated_at = now() WHERE id = 1"),
        {"cursor": cursor},
    )
    db.commit()


def _file_already_ingested(db, dropbox_file_id: str, content_hash: str) -> bool:
    return (
        db.query(Document)
        .filter(
            (Document.dropbox_file_id == dropbox_file_id)
            | (Document.content_hash == content_hash)
        )
        .first()
        is not None
    )


def run() -> None:
    db = SessionLocal()
    storage = StorageService()
    dropbox_svc = DropboxService()

    ingested = 0
    skipped = 0

    try:
        cursor = _get_cursor(db)
        logger.info("Starting Dropbox sync (cursor=%s)", "fresh" if not cursor else "incremental")

        files, new_cursor = dropbox_svc.list_new_files(cursor)

        for entry in files:
            ext = Path(entry.name).suffix.lower()
            if ext not in ALLOWED_EXTENSIONS:
                logger.debug("Skipping unsupported file type: %s", entry.name)
                continue

            content_hash = entry.content_hash  # Dropbox-computed SHA-256 of content blocks

            if _file_already_ingested(db, entry.id, content_hash):
                logger.info("Duplicate — skipping: %s (%s)", entry.name, entry.id)
                skipped += 1
                continue

            try:
                buf = dropbox_svc.download_file(entry.path_lower)
                raw_bytes = buf.read()
                file_size = len(raw_bytes)

                # Use our own hash as secondary check (Dropbox hash is primary)
                local_hash = hashlib.sha256(raw_bytes).hexdigest()

                # Persist to configured storage (local / Render disk / S3)
                unique_filename = f"{uuid.uuid4()}{ext}"
                content_type = mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
                storage.save_file_bytes_sync(raw_bytes, unique_filename, content_type)

                # Create document record in QUEUED state
                doc = Document(
                    filename=entry.name,
                    file_path=unique_filename,
                    file_size=file_size,
                    status=DocumentStatus.QUEUED,
                    dropbox_file_id=entry.id,
                    content_hash=local_hash,
                )
                db.add(doc)
                db.commit()
                db.refresh(doc)

                # Dispatch to existing Celery pipeline
                from worker import process_document_task
                process_document_task.delay(doc.id)

                logger.info("Ingested: %s → doc_id=%d", entry.name, doc.id)
                ingested += 1

            except Exception as exc:
                db.rollback()
                logger.error("Failed to ingest %s: %s", entry.name, exc)
                # Continue with remaining files rather than aborting the whole run

        _save_cursor(db, new_cursor)
        logger.info(
            "Sync complete — ingested=%d skipped=%d new_cursor=%s",
            ingested,
            skipped,
            new_cursor[:20] + "...",
        )

    except Exception as exc:
        logger.error("Dropbox ingest job failed: %s", exc)
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    run()
