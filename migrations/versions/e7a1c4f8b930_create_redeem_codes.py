"""create redeem codes tables

Revision ID: e7a1c4f8b930
Revises: d5b8e6f13a44
Create Date: 2026-07-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'e7a1c4f8b930'
down_revision: Union[str, Sequence[str], None] = 'd5b8e6f13a44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'redeem_codes',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('credits', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('grants_premium', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('max_uses', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('times_used', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_redeem_codes_code'), 'redeem_codes', ['code'], unique=True)

    op.create_table(
        'redeem_code_uses',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('code_id', sa.Uuid(), nullable=False),
        sa.Column('user_id', sa.Uuid(), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['code_id'], ['redeem_codes.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_redeem_code_uses_code_id'), 'redeem_code_uses', ['code_id'], unique=False)
    op.create_index(op.f('ix_redeem_code_uses_user_id'), 'redeem_code_uses', ['user_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_redeem_code_uses_user_id'), table_name='redeem_code_uses')
    op.drop_index(op.f('ix_redeem_code_uses_code_id'), table_name='redeem_code_uses')
    op.drop_table('redeem_code_uses')
    op.drop_index(op.f('ix_redeem_codes_code'), table_name='redeem_codes')
    op.drop_table('redeem_codes')
