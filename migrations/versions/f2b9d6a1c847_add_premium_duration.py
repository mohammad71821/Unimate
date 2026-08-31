"""add premium duration support

Revision ID: f2b9d6a1c847
Revises: e7a1c4f8b930
Create Date: 2026-07-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f2b9d6a1c847'
down_revision: Union[str, Sequence[str], None] = 'e7a1c4f8b930'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('premium_until', sa.DateTime(timezone=True), nullable=True))
    op.add_column('redeem_codes', sa.Column('premium_days', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('redeem_codes', 'premium_days')
    op.drop_column('users', 'premium_until')
