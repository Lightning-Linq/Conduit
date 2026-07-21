"""seller monetization schema: skills.is_active, executions.fee_invoice_source, provider_subscriptions

Revision ID: b7a91c4e2f03
Revises: d2c4f6a8b0e1
Create Date: 2026-07-21 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b7a91c4e2f03"
down_revision: Union[str, None] = "d2c4f6a8b0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Listing gate: skills hidden from discovery (and blocked from execution)
    # when a provider's subscription lapses over the free quota.
    # skills.is_active has existed since the initial schema but was unmapped by
    # the model and had NO server default (a latent NOT NULL trap on insert).
    # Set the default; the model now maps it.
    op.alter_column(
        "skills",
        "is_active",
        server_default=sa.text("true"),
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )

    # Which wallet issued the fee invoice ("local" | "platform"); NULL on
    # legacy rows means local. Confirm verifies against the issuing wallet.
    op.add_column(
        "skill_executions",
        sa.Column("fee_invoice_source", sa.String(length=10), nullable=True),
    )

    op.create_table(
        "provider_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_name", sa.String(length=255), nullable=False),
        sa.Column("tier", sa.String(length=20), server_default="pro", nullable=False),
        sa.Column("paid_until", sa.DateTime(timezone=True), nullable=True),
        # The one pending (unconfirmed) subscription invoice, if any.
        sa.Column("invoice_payment_hash", sa.String(length=64), nullable=True),
        sa.Column("invoice_payment_request", sa.Text(), nullable=True),
        sa.Column("invoice_amount_sats", sa.BigInteger(), nullable=True),
        sa.Column("invoice_period", sa.String(length=10), nullable=True),
        sa.Column("invoice_source", sa.String(length=10), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_name", name="uq_provider_subscription"),
    )
    op.create_index(
        op.f("ix_provider_subscriptions_provider_name"),
        "provider_subscriptions",
        ["provider_name"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_provider_subscriptions_provider_name"),
        table_name="provider_subscriptions",
    )
    op.drop_table("provider_subscriptions")
    op.drop_column("skill_executions", "fee_invoice_source")
    # is_active predates this migration (initial schema); only the default is ours.
    op.alter_column(
        "skills",
        "is_active",
        server_default=None,
        existing_type=sa.Boolean(),
        existing_nullable=False,
    )
