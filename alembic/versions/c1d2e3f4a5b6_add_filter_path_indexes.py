"""Add filter-path composite indexes for state, client, and review-queue queries

Revision ID: c1d2e3f4a5b6
Revises: a9f1b3c2d8e4
Create Date: 2026-06-12

Uses CREATE INDEX CONCURRENTLY so Postgres does not hold an exclusive table lock
during index build. CONCURRENTLY cannot run inside a transaction, so Alembic's
automatic transaction is disabled for this migration.
"""

from alembic import op

revision = "c1d2e3f4a5b6"
down_revision = "a9f1b3c2d8e4"
branch_labels = None
depends_on = None


def upgrade():
    # CONCURRENTLY requires autocommit — execute raw SQL outside the default transaction
    connection = op.get_bind()
    connection.execution_options(isolation_level="AUTOCOMMIT")

    connection.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_state_status "
        "ON documents (state, status)"
    )
    connection.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_client_canonical_status "
        "ON documents (client_canonical, status)"
    )
    connection.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_needs_review_status "
        "ON documents (needs_review, status)"
    )
    connection.execute(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_needs_date_review_status "
        "ON documents (needs_date_review, status)"
    )


def downgrade():
    connection = op.get_bind()
    connection.execution_options(isolation_level="AUTOCOMMIT")

    connection.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_needs_date_review_status")
    connection.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_needs_review_status")
    connection.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_client_canonical_status")
    connection.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_state_status")
