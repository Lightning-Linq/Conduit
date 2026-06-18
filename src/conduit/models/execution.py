"""Skill execution model — tracks when an agent uses a skill."""

import enum
import uuid

from sqlalchemy import (
    BigInteger,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from conduit.models.base import Base


class ExecutionStatus(str, enum.Enum):
    PENDING_PAYMENT = "pending_payment"
    PAYMENT_RECEIVED = "payment_received"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class SkillExecution(Base):
    """
    Records a skill execution between two agents.

    Non-custodial: the payment_hash and preimage prove the payment
    happened directly between consumer and provider on Lightning.
    Conduit stores the proof but never held the funds.
    """

    __tablename__ = "skill_executions"

    # Which skill was executed
    skill_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("skills.id"), index=True
    )

    # Consumer (the agent paying for the skill)
    consumer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Payment proof (Lightning preimage proves payment happened)
    payment_hash: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    payment_preimage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    amount_sats: Mapped[int] = mapped_column(BigInteger)

    # Platform fee (two-invoice model: consumer pays provider + platform separately)
    platform_fee_sats: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")
    fee_payment_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fee_payment_request: Mapped[str | None] = mapped_column(Text, nullable=True)
    fee_settled: Mapped[bool] = mapped_column(default=False, server_default="false")

    # Federation (phase 5): the consumer's Nostr key (rater identity) captured at
    # request time, and the provider's payer-binding signature minted at confirm.
    # Both nullable — executions without them simply aren't federatable.
    payer_pubkey: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_binding_sig: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Execution data
    input_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_data: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[ExecutionStatus] = mapped_column(
        Enum(ExecutionStatus), default=ExecutionStatus.PENDING_PAYMENT
    )
    execution_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    skill: Mapped["Skill"] = relationship(back_populates="executions")  # noqa: F821
    ratings: Mapped[list["Rating"]] = relationship(back_populates="execution", lazy="selectin")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Execution {self.id} skill={self.skill_id} [{self.status}]>"
