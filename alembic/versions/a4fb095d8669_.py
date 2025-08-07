"""empty message

Revision ID: a4fb095d8669
Revises: 43a3945b2fbe, 86a7849a0be1
Create Date: 2025-08-07 15:50:54.040040

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4fb095d8669'
down_revision: Union[str, None] = ('43a3945b2fbe', '86a7849a0be1')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
