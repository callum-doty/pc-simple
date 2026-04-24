"""add_dropbox_ingestion_tables

Revision ID: a1b2c3d4e5f6
Revises: e8f9c12a3d56
Create Date: 2026-04-24 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "e8f9c12a3d56"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("dropbox_file_id", sa.String(255), nullable=True, unique=True),
    )
    op.add_column(
        "documents",
        sa.Column("content_hash", sa.String(64), nullable=True),
    )
    op.create_index(
        "idx_documents_dropbox_file_id",
        "documents",
        ["dropbox_file_id"],
        unique=True,
        postgresql_where=sa.text("dropbox_file_id IS NOT NULL"),
    )
    op.create_index(
        "idx_documents_content_hash",
        "documents",
        ["content_hash"],
        postgresql_where=sa.text("content_hash IS NOT NULL"),
    )

    op.create_table(
        "dropbox_sync_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
        ),
    )
    # Seed the single state row
    op.execute("INSERT INTO dropbox_sync_state (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("dropbox_sync_state")
    op.drop_index("idx_documents_content_hash", table_name="documents")
    op.drop_index("idx_documents_dropbox_file_id", table_name="documents")
    op.drop_column("documents", "content_hash")
    op.drop_column("documents", "dropbox_file_id")
