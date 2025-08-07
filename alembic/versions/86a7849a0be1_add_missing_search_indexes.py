"""add_missing_search_indexes

Revision ID: 86a7849a0be1
Revises: 72c808426f98
Create Date: 2025-08-07 15:46:03.123456

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "86a7849a0be1"
down_revision = "72c808426f98"
branch_labels = None
depends_on = None


def upgrade():
    # Enable pg_trgm extension for trigram indexes
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm;")

    # Index for filename pattern matching
    op.create_index(
        "idx_documents_filename_trgm",
        "documents",
        ["filename"],
        postgresql_using="gin",
        postgresql_ops={"filename": "gin_trgm_ops"},
    )

    # Composite index for efficient filtering and sorting
    op.create_index(
        "idx_documents_status_created_at", "documents", ["status", "created_at"]
    )

    # Composite index for efficient joins in taxonomy filtering
    op.create_index(
        "idx_document_taxonomy_map_efficient",
        "document_taxonomy_map",
        ["document_id", "taxonomy_term_id"],
    )


def downgrade():
    op.drop_index(
        "idx_document_taxonomy_map_efficient", table_name="document_taxonomy_map"
    )
    op.drop_index("idx_documents_status_created_at", table_name="documents")
    op.drop_index("idx_documents_filename_trgm", table_name="documents")
