"""add federation fields to skill_executions

Revision ID: 5115ab669040
Revises: 320fbf512bda
Create Date: 2026-06-08 10:16:21.783392
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5115ab669040'
down_revision: Union[str, None] = '320fbf512bda'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "skill_executions",
        sa.Column("payer_pubkey", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "skill_executions",
        sa.Column("provider_binding_sig", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("skill_executions", "provider_binding_sig")
    op.drop_column("skill_executions", "payer_pubkey")
