"""add extracted_text and processing_status to notes

Revision ID: f6d43b30a863
Revises: 52717eab523a
Create Date: 2026-07-20 23:04:23.127471

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f6d43b30a863'
down_revision: Union[str, Sequence[str], None] = 'bb6eb1ea0b6f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('notes', sa.Column('extracted_text', sa.Text(), nullable=True))
    op.add_column(
        'notes',
        sa.Column('processing_status', sa.String(length=50), nullable=False, server_default='pending'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('notes', 'processing_status')
    op.drop_column('notes', 'extracted_text')
