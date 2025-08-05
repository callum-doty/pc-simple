"""Add ts_vector column to documents table

Revision ID: 72c808426f98
Revises: 9a9ed56149a0
Create Date: 2025-07-31 14:03:57.451700

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "72c808426f98"
down_revision: Union[str, None] = "9a9ed56149a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # op.add_column(
    #     "documents",
    #     sa.Column(
    #         "ts_vector",
    #         sa.dialects.postgresql.TSVECTOR(),
    #         sa.Computed(
    #             "to_tsvector('english', coalesce(filename, '') || ' ' || coalesce(extracted_text, ''))",
    #             persisted=True,
    #         ),
    #         nullable=True,
    #     ),
    # )
    # op.create_index(
    #     "idx_documents_ts_vector",
    #     "documents",
    #     ["ts_vector"],
    #     unique=False,
    #     postgresql_using="gin",
    # )
    pass


def downgrade() -> None:
    # op.drop_index("idx_documents_ts_vector", table_name="documents")
    # op.drop_column("documents", "ts_vector")
    pass
