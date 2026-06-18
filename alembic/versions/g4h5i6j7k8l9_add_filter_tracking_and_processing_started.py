"""Add filter tracking to search_queries and processing_started_at to documents

Revision ID: g4h5i6j7k8l9
Revises: c1d2e3f4a5b6
Create Date: 2026-06-18

Changes:
- search_queries: add filter_client, filter_state, filter_date_year, result_count
  so we can analyse which filters users apply and detect zero-result searches.
- documents: add processing_started_at so avg processing time measures actual
  worker time (processing_started_at → processed_at) instead of the misleading
  upload-to-completion figure (created_at → processed_at).
"""

from alembic import op
import sqlalchemy as sa

revision = "g4h5i6j7k8l9"
down_revision = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- search_queries filter columns --
    op.add_column(
        "search_queries",
        sa.Column("filter_client", sa.String(), nullable=True),
    )
    op.add_column(
        "search_queries",
        sa.Column("filter_state", sa.String(), nullable=True),
    )
    op.add_column(
        "search_queries",
        sa.Column("filter_date_year", sa.Integer(), nullable=True),
    )
    op.add_column(
        "search_queries",
        sa.Column("result_count", sa.Integer(), nullable=True),
    )

    # Indexes for filter-usage aggregation queries
    op.create_index("ix_search_queries_filter_client", "search_queries", ["filter_client"])
    op.create_index("ix_search_queries_filter_state", "search_queries", ["filter_state"])

    # -- documents processing_started_at --
    op.add_column(
        "documents",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "processing_started_at")

    op.drop_index("ix_search_queries_filter_state", table_name="search_queries")
    op.drop_index("ix_search_queries_filter_client", table_name="search_queries")
    op.drop_column("search_queries", "result_count")
    op.drop_column("search_queries", "filter_date_year")
    op.drop_column("search_queries", "filter_state")
    op.drop_column("search_queries", "filter_client")
