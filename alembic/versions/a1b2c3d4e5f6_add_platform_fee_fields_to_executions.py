"""add platform fee fields to executions

Revision ID: a1b2c3d4e5f6
Revises: 1eb870646862
Create Date: 2026-05-27
"""
from alembic import op
import sqlalchemy as sa

revision = "a1b2c3d4e5f6"
down_revision = "1eb870646862"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "skill_executions",
        sa.Column("platform_fee_sats", sa.BigInteger(), server_default="0", nullable=False),
    )
    op.add_column(
        "skill_executions",
        sa.Column("fee_payment_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "skill_executions",
        sa.Column("fee_payment_request", sa.Text(), nullable=True),
    )
    op.add_column(
        "skill_executions",
        sa.Column("fee_settled", sa.Boolean(), server_default="false", nullable=False),
    )


def downgrade() -> None:
    op.drop_column("skill_executions", "fee_settled")
    op.drop_column("skill_executions", "fee_payment_request")
    op.drop_column("skill_executions", "fee_payment_hash")
    op.drop_column("skill_executions", "platform_fee_sats")
