"""Remote execution model — Federation #3, the broker's side of a cross-node buy.

When an agent on THIS node buys a skill hosted by a peer, this node is a broker,
not a seller: it relays the peer's invoice to the consumer, the consumer pays the
peer DIRECTLY over Lightning, and this node relays the confirm back. No funds ever
touch this node, so the row below is a routing record (which peer, which execution
id over there) plus the payment proof the consumer produced.

Why a separate table rather than SkillExecution: skill_executions.skill_id is a
NOT NULL FK to skills.id and a remote skill has no local Skill row. Weakening that
FK would loosen a constraint on the busiest money table, and writing a shadow Skill
row would put a skill this node cannot execute into local discovery.
"""

from sqlalchemy import BigInteger, Enum, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from conduit.models.base import Base
from conduit.models.execution import ExecutionStatus


class RemoteExecution(Base):
    """A skill execution brokered to a federation peer."""

    __tablename__ = "remote_executions"

    # The skill as the PEER knows it. Deliberately a plain string, not a FK:
    # it names a row in the peer's registry, mirrored locally only in cached_skills.
    remote_skill_id: Mapped[str] = mapped_column(String(255), index=True)

    # Which peer hosts it, and the execution id it issued. The peer URL is only
    # ever one this operator listed in FEDERATION_PEERS (see federation_execution.
    # resolve_peer_url) — a cached listing alone never picks the target host.
    peer_url: Mapped[str] = mapped_column(String(512))
    remote_execution_id: Mapped[str] = mapped_column(String(64))

    # Who bought it here, and (optionally) their Nostr key so the peer's binding
    # signature can be relayed back for a federated rating.
    consumer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payer_pubkey: Mapped[str | None] = mapped_column(String(64), nullable=True)
    input_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # What the consumer was quoted. Verified against the cached listing price and
    # against the peer's own invoice before the consumer ever sees it.
    amount_sats: Mapped[int] = mapped_column(BigInteger, default=0)
    platform_fee_sats: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")

    # Payment proof — the hashes of the PEER's invoices. This node holds no funds.
    payment_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    fee_payment_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Same lifecycle vocabulary as a local execution.
    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus), default=ExecutionStatus.PENDING_PAYMENT
    )
    output_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        # Idempotency key: a retried broker call cannot create a second local row
        # for the same purchase on the same peer.
        UniqueConstraint("peer_url", "remote_execution_id", name="uq_remote_execution_peer_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<RemoteExecution {self.id} skill={self.remote_skill_id} "
            f"peer={self.peer_url} [{self.status}]>"
        )
