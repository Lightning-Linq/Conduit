"""add cached_skills catalog (federation #2)

Revision ID: d2c4f6a8b0e1
Revises: fad118a12739
Create Date: 2026-06-22 11:30:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d2c4f6a8b0e1"
down_revision: Union[str, None] = "fad118a12739"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "cached_skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("provider_pubkey", sa.String(length=64), nullable=False),
        sa.Column("skill_id", sa.String(length=255), nullable=False),
        sa.Column("event_id", sa.String(length=64), nullable=False),
        sa.Column("event_created_at", sa.BigInteger(), nullable=False),
        sa.Column("origin", sa.String(length=16), nullable=False),
        sa.Column("source_id", sa.String(length=512), nullable=True),
        sa.Column("provider_name", sa.String(length=255), nullable=True),
        sa.Column("provider_lightning_address", sa.String(length=255), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("category", sa.String(length=100), nullable=True),
        sa.Column("tags", sa.Text(), nullable=True),
        sa.Column("price_sats", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("endpoint_url", sa.String(length=512), nullable=True),
        sa.Column("input_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("output_schema", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider_pubkey", "skill_id", name="uq_cached_skill_coord"),
    )
    op.create_index(
        op.f("ix_cached_skills_provider_pubkey"),
        "cached_skills",
        ["provider_pubkey"],
        unique=False,
    )
    op.create_index(op.f("ix_cached_skills_name"), "cached_skills", ["name"], unique=False)
    op.create_index(
        op.f("ix_cached_skills_category"), "cached_skills", ["category"], unique=False
    )
    op.create_index(
        "ix_cached_skills_provider_skill",
        "cached_skills",
        ["provider_pubkey", "skill_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cached_skills_provider_skill", table_name="cached_skills")
    op.drop_index(op.f("ix_cached_skills_category"), table_name="cached_skills")
    op.drop_index(op.f("ix_cached_skills_name"), table_name="cached_skills")
    op.drop_index(op.f("ix_cached_skills_provider_pubkey"), table_name="cached_skills")
    op.drop_table("cached_skills")
