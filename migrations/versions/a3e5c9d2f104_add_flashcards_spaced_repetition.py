"""add flashcards spaced repetition

Revision ID: a3e5c9d2f104
Revises: f2b9d6a1c847
Create Date: 2026-07-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a3e5c9d2f104'
down_revision: Union[str, Sequence[str], None] = 'f2b9d6a1c847'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('users', sa.Column('last_flashcard_nudge_date', sa.String(length=10), nullable=True))

    op.create_table(
        'flashcards',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('note_id', sa.Uuid(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('answer', sa.Text(), nullable=False),
        sa.Column('interval_days', sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('ease_factor', sa.Float(), nullable=False, server_default='2.5'),
        sa.Column('repetitions', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('next_review_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_reviewed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['note_id'], ['notes.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_flashcards_owner_id'), 'flashcards', ['owner_id'], unique=False)
    op.create_index(op.f('ix_flashcards_note_id'), 'flashcards', ['note_id'], unique=False)
    op.create_index(op.f('ix_flashcards_next_review_at'), 'flashcards', ['next_review_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_flashcards_next_review_at'), table_name='flashcards')
    op.drop_index(op.f('ix_flashcards_note_id'), table_name='flashcards')
    op.drop_index(op.f('ix_flashcards_owner_id'), table_name='flashcards')
    op.drop_table('flashcards')
    op.drop_column('users', 'last_flashcard_nudge_date')
