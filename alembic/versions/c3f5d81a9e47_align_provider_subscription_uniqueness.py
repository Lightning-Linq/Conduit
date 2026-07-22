"""align provider_subscriptions uniqueness with the model

b7a91c4e2f03 created a unique CONSTRAINT plus a separate non-unique index
on provider_subscriptions.provider_name. The model declares
`unique=True, index=True`, which SQLAlchemy renders as a single UNIQUE
INDEX. Uniqueness was enforced either way, but the shapes differed (so
`alembic check` failed) and the column carried a redundant second index.

Revision ID: c3f5d81a9e47
Revises: b7a91c4e2f03
Create Date: 2026-07-22 13:20:00.000000
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3f5d81a9e47"
down_revision: Union[str, None] = "b7a91c4e2f03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Postgres DDL is transactional, so uniqueness is never unenforced
    # mid-migration: the unique index exists before the constraint goes.
    op.drop_index(
        op.f("ix_provider_subscriptions_provider_name"),
        table_name="provider_subscriptions",
    )
    op.create_index(
        op.f("ix_provider_subscriptions_provider_name"),
        "provider_subscriptions",
        ["provider_name"],
        unique=True,
    )
    op.drop_constraint(
        "uq_provider_subscription", "provider_subscriptions", type_="unique"
    )


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_provider_subscription", "provider_subscriptions", ["provider_name"]
    )
    op.drop_index(
        op.f("ix_provider_subscriptions_provider_name"),
        table_name="provider_subscriptions",
    )
    op.create_index(
        op.f("ix_provider_subscriptions_provider_name"),
        "provider_subscriptions",
        ["provider_name"],
        unique=False,
    )
