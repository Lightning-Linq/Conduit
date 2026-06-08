"""add federated_attestations cache

Revision ID: 320fbf512bda
Revises: a1b2c3d4e5f6
Create Date: 2026-06-05 11:59:56.989806
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '320fbf512bda'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "federated_attestations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("skill_id", sa.String(length=255), nullable=False),
        sa.Column("provider_pubkey", sa.String(length=64), nullable=False),
        sa.Column("rater_pubkey", sa.String(length=64), nullable=False),
        sa.Column("payment_hash", sa.String(length=64), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("attestation_created_at", sa.BigInteger(), nullable=False),
        sa.Column("raw_event", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.CheckConstraint("score >= 1 AND score <= 5", name="valid_attestation_score"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_federated_attestations_event_id"),
        "federated_attestations",
        ["event_id"],
        unique=True,
    )
    op.create_index(
        op.f("ix_federated_attestations_provider_pubkey"),
        "federated_attestations",
        ["provider_pubkey"],
        unique=False,
    )
    op.create_index(
        "ix_fed_att_skill_provider",
        "federated_attestations",
        ["skill_id", "provider_pubkey"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_fed_att_skill_provider", table_name="federated_attestations")
    op.drop_index(
        op.f("ix_federated_attestations_provider_pubkey"),
        table_name="federated_attestations",
    )
    op.drop_index(
        op.f("ix_federated_attestations_event_id"),
        table_name="federated_attestations",
    )
    op.drop_table("federated_attestations")
