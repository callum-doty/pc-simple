"""add_composite_indexes_for_performance

Revision ID: d586c77b1fc4
Revises: 86a7849a0be1
Create Date: 2025-01-11 12:18:17.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "d586c77b1fc4"
down_revision = "86a7849a0be1"
branch_labels = None
depends_on = None


def upgrade():
    """Add composite indexes for better query performance"""
    # Add composite indexes for common query patterns
    op.create_index("idx_status_created", "documents", ["status", "created_at"])
    op.create_index("idx_status_updated", "documents", ["status", "updated_at"])
    op.create_index("idx_status_processed", "documents", ["status", "processed_at"])
    op.create_index("idx_filename_status", "documents", ["filename", "status"])


def downgrade():
    """Remove composite indexes"""
    op.drop_index("idx_filename_status", table_name="documents")
    op.drop_index("idx_status_processed", table_name="documents")
    op.drop_index("idx_status_updated", table_name="documents")
    op.drop_index("idx_status_created", table_name="documents")
