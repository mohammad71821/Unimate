"""add is_active to courses

Revision ID: e4b1f9c27a60
Revises: d8c2a7f31b55
Create Date: 2026-10-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e4b1f9c27a60'
down_revision: Union[str, Sequence[str], None] = 'd8c2a7f31b55'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'courses',
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('courses', 'is_active')
