"""add invoice_tier to provider_subscriptions

The pending subscription invoice now records which tier it purchases
("starter" | "pro"), applied to `tier` only at confirm (after settlement).
Additive and nullable — legacy rows keep NULL and read as Pro via the
tier column's own default.

Revision ID: e8b2f1a4c6d9
Revises: c3f5d81a9e47
Create Date: 2026-07-23 11:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e8b2f1a4c6d9"
down_revision: Union[str, None] = "c3f5d81a9e47"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "provider_subscriptions",
        sa.Column("invoice_tier", sa.String(length=20), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("provider_subscriptions", "invoice_tier")
