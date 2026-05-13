"""add embedding versioning columns

Revision ID: b3c4d5e6f7a8
Revises: f3a8b2c1d9e7
Create Date: 2026-05-12 00:00:00.000000

"""

from alembic import op
import sqlalchemy as sa

revision = "b3c4d5e6f7a8"
down_revision = "f3a8b2c1d9e7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("embedding_model", sa.String(100), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("embedding_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "documents",
        sa.Column("embedding_provenance", sa.dialects.postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "embedding_provenance")
    op.drop_column("documents", "embedding_version")
    op.drop_column("documents", "embedding_model")
