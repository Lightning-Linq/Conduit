"""add remote_executions table (Federation #3)

Broker-side record of an execution this node relayed to a federation peer. The
node holds no funds for these: it stores which peer, the peer's execution id, and
the payment hashes of the peer's invoices that the consumer paid directly.

Additive and reversible — no existing table is touched.

Revision ID: b7d3e91c204f
Revises: e8b2f1a4c6d9
Create Date: 2026-07-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'b7d3e91c204f'
down_revision: Union[str, None] = 'e8b2f1a4c6d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'remote_executions',
        sa.Column('remote_skill_id', sa.String(length=255), nullable=False),
        sa.Column('peer_url', sa.String(length=512), nullable=False),
        sa.Column('remote_execution_id', sa.String(length=64), nullable=False),
        sa.Column('consumer_name', sa.String(length=255), nullable=True),
        sa.Column('payer_pubkey', sa.String(length=64), nullable=True),
        sa.Column('input_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('amount_sats', sa.BigInteger(), nullable=False),
        sa.Column('platform_fee_sats', sa.BigInteger(), server_default='0', nullable=False),
        sa.Column('payment_hash', sa.String(length=64), nullable=True),
        sa.Column('fee_payment_hash', sa.String(length=64), nullable=True),
        # Reuses the existing executionstatus enum type created with skill_executions.
        # create_type=False so this migration does not try to CREATE TYPE a second time.
        sa.Column(
            'status',
            postgresql.ENUM(
                'PENDING_PAYMENT',
                'PAYMENT_RECEIVED',
                'EXECUTING',
                'COMPLETED',
                'FAILED',
                'REFUNDED',
                name='executionstatus',
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column('output_data', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('id', sa.UUID(), nullable=False),
        sa.Column(
            'created_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.Column(
            'updated_at',
            sa.DateTime(timezone=True),
            server_default=sa.text('now()'),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint('id'),
        # One local row per (peer, that peer's execution id) — a retried broker
        # call cannot create a second record for the same purchase.
        sa.UniqueConstraint('peer_url', 'remote_execution_id', name='uq_remote_execution_peer_id'),
    )
    op.create_index(
        op.f('ix_remote_executions_remote_skill_id'),
        'remote_executions',
        ['remote_skill_id'],
        unique=False,
    )
    op.create_index(
        op.f('ix_remote_executions_payment_hash'),
        'remote_executions',
        ['payment_hash'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f('ix_remote_executions_payment_hash'), table_name='remote_executions')
    op.drop_index(op.f('ix_remote_executions_remote_skill_id'), table_name='remote_executions')
    op.drop_table('remote_executions')
    # The executionstatus enum type is left alone: skill_executions still uses it.
