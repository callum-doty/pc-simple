"""add_dropbox_ingestion_tables

Revision ID: a1b2c3d4e5f6
Revises: e8f9c12a3d56
Create Date: 2026-04-24 00:00:00.000000

"""

from alembic import op
from sqlalchemy import text

revision = "a1b2c3d4e5f6"
down_revision = "e8f9c12a3d56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Use IF NOT EXISTS throughout so this is safe to re-run if _ensure_schema
    # already created these from the cron job before the migration ran.
    op.execute(text("""
        ALTER TABLE documents
            ADD COLUMN IF NOT EXISTS dropbox_file_id VARCHAR(255),
            ADD COLUMN IF NOT EXISTS content_hash VARCHAR(64)
    """))

    # Partial unique index — only enforces uniqueness on non-null values
    op.execute(text("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_dropbox_file_id
            ON documents (dropbox_file_id)
            WHERE dropbox_file_id IS NOT NULL
    """))

    op.execute(text("""
        CREATE INDEX IF NOT EXISTS idx_documents_content_hash
            ON documents (content_hash)
            WHERE content_hash IS NOT NULL
    """))

    op.execute(text("""
        CREATE TABLE IF NOT EXISTS dropbox_sync_state (
            id INTEGER PRIMARY KEY,
            cursor TEXT,
            updated_at TIMESTAMPTZ DEFAULT now()
        )
    """))

    op.execute(text("""
        INSERT INTO dropbox_sync_state (id) VALUES (1)
        ON CONFLICT (id) DO NOTHING
    """))


def downgrade() -> None:
    op.execute(text("DROP TABLE IF EXISTS dropbox_sync_state"))
    op.execute(text("DROP INDEX IF EXISTS idx_documents_content_hash"))
    op.execute(text("DROP INDEX IF EXISTS idx_documents_dropbox_file_id"))
    op.execute(text("ALTER TABLE documents DROP COLUMN IF EXISTS content_hash"))
    op.execute(text("ALTER TABLE documents DROP COLUMN IF EXISTS dropbox_file_id"))
