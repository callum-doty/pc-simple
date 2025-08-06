"""empty message

Revision ID: a4fb095d8669
Revises: 65d45777a561, 76ce7437e49a
Create Date: 2025-08-06 12:43:29.068601

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4fb095d8669'
down_revision: Union[str, None] = ('65d45777a561', '76ce7437e49a')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
