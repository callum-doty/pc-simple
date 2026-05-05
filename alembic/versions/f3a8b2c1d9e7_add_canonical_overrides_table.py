"""Add canonical_overrides table

Revision ID: f3a8b2c1d9e7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-05

Replaces canonical_map_template.csv as the single source of truth for
client name overrides. Enables real-time corrections, targeted reprocessing,
and audit history without file I/O or environment drift.

To seed from an existing CSV:
    python feature_extraction/seed_canonical_overrides.py \\
        --csv feature_extraction/canonical_map_template.csv \\
        --db-url "postgresql://..."
"""

from alembic import op
import sqlalchemy as sa

revision = "f3a8b2c1d9e7"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "canonical_overrides",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("client_clean_v1", sa.Text(), nullable=False),
        sa.Column("client_canonical", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_clean_v1", name="uq_canonical_overrides_clean_v1"),
    )
    op.create_index(
        "idx_canonical_overrides_client_clean_v1",
        "canonical_overrides",
        ["client_clean_v1"],
    )


def downgrade() -> None:
    op.drop_index("idx_canonical_overrides_client_clean_v1", table_name="canonical_overrides")
    op.drop_table("canonical_overrides")
