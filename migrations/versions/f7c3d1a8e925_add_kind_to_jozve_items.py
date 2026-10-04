"""add kind to jozve items

Revision ID: f7c3d1a8e925
Revises: e4b1f9c27a60
Create Date: 2026-10-04 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'f7c3d1a8e925'
down_revision: Union[str, Sequence[str], None] = 'e4b1f9c27a60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'jozve_items',
        sa.Column('kind', sa.String(length=20), nullable=False, server_default='summary'),
    )


def downgrade() -> None:
    op.drop_column('jozve_items', 'kind')
