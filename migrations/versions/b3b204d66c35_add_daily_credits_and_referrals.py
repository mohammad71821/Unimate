"""add daily credits and referrals

Revision ID: b3b204d66c35
Revises: a3e5c9d2f104
Create Date: 2026-08-01 21:07:20.958611

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b3b204d66c35'
down_revision: Union[str, Sequence[str], None] = 'a3e5c9d2f104'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("users", sa.Column("daily_credits_used", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("users", sa.Column("daily_credits_date", sa.String(length=10), nullable=True))

    op.create_table(
        "referrals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("referrer_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("referred_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id"), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_referrals_referrer_id", "referrals", ["referrer_id"])
    op.create_index("ix_referrals_referred_id", "referrals", ["referred_id"])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_referrals_referred_id", table_name="referrals")
    op.drop_index("ix_referrals_referrer_id", table_name="referrals")
    op.drop_table("referrals")
    op.drop_column("users", "daily_credits_date")
    op.drop_column("users", "daily_credits_used")
