"""add courses and jozve items

Revision ID: d8c2a7f31b55
Revises: b3b204d66c35
Create Date: 2026-10-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd8c2a7f31b55'
down_revision: Union[str, Sequence[str], None] = 'b3b204d66c35'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'courses',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('owner_id', 'name', name='uq_courses_owner_name'),
    )
    op.create_index(op.f('ix_courses_owner_id'), 'courses', ['owner_id'], unique=False)

    op.create_table(
        'jozve_items',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('owner_id', sa.Uuid(), nullable=False),
        sa.Column('course_id', sa.Uuid(), nullable=False),
        sa.Column('note_id', sa.Uuid(), nullable=True),
        sa.Column('number', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('title', sa.String(length=300), nullable=True),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['course_id'], ['courses.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['note_id'], ['notes.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_jozve_items_owner_id'), 'jozve_items', ['owner_id'], unique=False)
    op.create_index(op.f('ix_jozve_items_course_id'), 'jozve_items', ['course_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_jozve_items_course_id'), table_name='jozve_items')
    op.drop_index(op.f('ix_jozve_items_owner_id'), table_name='jozve_items')
    op.drop_table('jozve_items')
    op.drop_index(op.f('ix_courses_owner_id'), table_name='courses')
    op.drop_table('courses')
