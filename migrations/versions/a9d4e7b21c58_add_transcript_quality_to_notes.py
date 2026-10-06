"""add transcript_quality to notes

Revision ID: a9d4e7b21c58
Revises: f7c3d1a8e925
Create Date: 2026-10-05 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a9d4e7b21c58'
down_revision: Union[str, Sequence[str], None] = 'f7c3d1a8e925'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('notes', sa.Column('transcript_quality', sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column('notes', 'transcript_quality')
