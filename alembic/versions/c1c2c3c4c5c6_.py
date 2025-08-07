"""empty message

Revision ID: c1c2c3c4c5c6
Revises: a4fb095d8669, b1b2b3b4b5b6
Create Date: 2025-08-07 16:01:13.421279

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1c2c3c4c5c6'
down_revision: Union[str, None] = ('a4fb095d8669', 'b1b2b3b4b5b6')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
