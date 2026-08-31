"""create reminders table

Revision ID: c3f7a4e91b22
Revises: a1c9f2e5d701
Create Date: 2026-07-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'c3f7a4e91b22'
down_revision: Union[str, Sequence[str], None] = 'a1c9f2e5d701'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'reminders',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('chat_id', sa.String(length=64), nullable=False),
        sa.Column('message', sa.Text(), nullable=False),
        sa.Column('remind_at', sa.DateTime(), nullable=False),
        sa.Column('sent', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_reminders_owner_id'), 'reminders', ['owner_id'])
    op.create_index(op.f('ix_reminders_remind_at'), 'reminders', ['remind_at'])
    op.create_index(op.f('ix_reminders_sent'), 'reminders', ['sent'])


def downgrade() -> None:
    op.drop_index(op.f('ix_reminders_sent'), table_name='reminders')
    op.drop_index(op.f('ix_reminders_remind_at'), table_name='reminders')
    op.drop_index(op.f('ix_reminders_owner_id'), table_name='reminders')
    op.drop_table('reminders')
