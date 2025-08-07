"""add_search_vector_column_to_documents

Revision ID: 43a3945b2fbe
Revises: 72c808426f98
Create Date: 2025-08-06 14:35:50.631999

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector


# revision identifiers, used by Alembic.
revision: str = "43a3945b2fbe"
down_revision: Union[str, None] = "72c808426f98"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("search_vector", Vector(1536), nullable=True))
    op.create_index(
        "idx_documents_search_vector",
        "documents",
        ["search_vector"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_with={"m": 16, "ef_construction": 64},
        postgresql_ops={"search_vector": "vector_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("idx_documents_search_vector", table_name="documents")
    op.drop_column("documents", "search_vector")
