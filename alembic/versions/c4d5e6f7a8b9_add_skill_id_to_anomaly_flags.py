"""add skill_id to anomaly_flags

Lets a flag (including REQ-09 consumer reports) attach to a specific skill, not
just an execution, so a listing can be reported even without a purchase.

Revision ID: c4d5e6f7a8b9
Revises: 5115ab669040
Create Date: 2026-06-17
"""
from alembic import op
import sqlalchemy as sa

revision = "c4d5e6f7a8b9"
down_revision = "5115ab669040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "anomaly_flags",
        sa.Column("skill_id", sa.String(36), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("anomaly_flags", "skill_id")
