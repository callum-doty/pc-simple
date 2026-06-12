"""Add filter-path composite indexes for state, client, and review-queue queries

Revision ID: c1d2e3f4a5b6
Revises: a9f1b3c2d8e4
Create Date: 2026-06-12
"""

from alembic import op

revision = "c1d2e3f4a5b6"
down_revision = "a9f1b3c2d8e4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("idx_state_status", "documents", ["state", "status"])
    op.create_index("idx_client_canonical_status", "documents", ["client_canonical", "status"])
    op.create_index("idx_needs_review_status", "documents", ["needs_review", "status"])
    op.create_index("idx_needs_date_review_status", "documents", ["needs_date_review", "status"])


def downgrade():
    op.drop_index("idx_needs_date_review_status", table_name="documents")
    op.drop_index("idx_needs_review_status", table_name="documents")
    op.drop_index("idx_client_canonical_status", table_name="documents")
    op.drop_index("idx_state_status", table_name="documents")
