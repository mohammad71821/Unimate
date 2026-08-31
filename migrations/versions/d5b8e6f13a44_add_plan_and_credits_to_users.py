"""add plan and credits to users

Revision ID: d5b8e6f13a44
Revises: c3f7a4e91b22
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd5b8e6f13a44'
down_revision: Union[str, Sequence[str], None] = 'c3f7a4e91b22'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('plan', sa.String(length=20), nullable=False, server_default='free'))
    op.add_column('users', sa.Column('credits', sa.Integer(), nullable=False, server_default='20'))


def downgrade() -> None:
    op.drop_column('users', 'credits')
    op.drop_column('users', 'plan')
