"""add embedding to notes

Revision ID: a1c9f2e5d701
Revises: b5ea440127a3
Create Date: 2026-07-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a1c9f2e5d701'
down_revision: Union[str, Sequence[str], None] = 'b5ea440127a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notes', sa.Column('embedding', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('notes', 'embedding')
