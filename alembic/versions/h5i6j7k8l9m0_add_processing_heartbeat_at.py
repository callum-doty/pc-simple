"""Add processing_heartbeat_at to documents

Revision ID: h5i6j7k8l9m0
Revises: g4h5i6j7k8l9
Create Date: 2026-06-19

Changes:
- documents: add processing_heartbeat_at (nullable DateTime with timezone).
  The worker updates this column periodically while a document is in PROCESSING
  status. The recovery scheduler uses it to detect zombie tasks — documents
  whose heartbeat has expired past (task_timeout + grace_period) are reset to
  QUEUED for reprocessing. See docs/architecture-fixes/FIX-001.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "h5i6j7k8l9m0"
down_revision = "g4h5i6j7k8l9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "processing_heartbeat_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Index to make the zombie-detection query fast:
    # SELECT * FROM documents WHERE status='PROCESSING' AND processing_heartbeat_at < $1
    op.create_index(
        "idx_processing_heartbeat",
        "documents",
        ["status", "processing_heartbeat_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("idx_processing_heartbeat", table_name="documents")
    op.drop_column("documents", "processing_heartbeat_at")
