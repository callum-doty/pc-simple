"""empty message

Revision ID: b1b2b3b4b5b6
Revises: 65d45777a561, 76ce7437e49a
Create Date: 2025-08-07 16:00:56.253262

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1b2b3b4b5b6'
down_revision: Union[str, None] = ('65d45777a561', '76ce7437e49a')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
