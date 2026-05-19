"""Add needs_date_review flag to documents

Revision ID: a9f1b3c2d8e4
Revises: f3a8b2c1d9e7
Create Date: 2026-05-18

Marks documents whose extracted date_created year falls outside the valid
range of 2019–2026 so they appear in the human review queue.
"""

from alembic import op
import sqlalchemy as sa

revision = "a9f1b3c2d8e4"
down_revision = "f3a8b2c1d9e7"
branch_labels = None
depends_on = None

VALID_YEAR_MIN = 2019
VALID_YEAR_MAX = 2026


def upgrade():
    op.add_column(
        "documents",
        sa.Column(
            "needs_date_review",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_documents_needs_date_review",
        "documents",
        ["needs_date_review"],
    )

    # Backfill: flag any existing document whose extracted year is out of range.
    op.execute(
        f"""
        UPDATE documents
        SET needs_date_review = true
        WHERE date_created IS NOT NULL
          AND (
            EXTRACT(year FROM date_created) < {VALID_YEAR_MIN}
            OR EXTRACT(year FROM date_created) > {VALID_YEAR_MAX}
          )
        """
    )


def downgrade():
    op.drop_index("ix_documents_needs_date_review", table_name="documents")
    op.drop_column("documents", "needs_date_review")
