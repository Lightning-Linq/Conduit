"""Payment model — outgoing Lightning payments made by agents."""

import enum
import uuid

from sqlalchemy import BigInteger, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from conduit.models.base import Base


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Payment(Base):
    """
    An outgoing Lightning payment initiated by an agent.

    Tracks the full lifecycle from initiation through settlement or failure.
    """

    __tablename__ = "payments"

    # Belongs to a wallet
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id"), index=True
    )

    # Payment details
    payment_request: Mapped[str] = mapped_column(Text)  # BOLT-11 invoice being paid
    payment_hash: Mapped[str] = mapped_column(String(64), index=True)
    preimage: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Amounts
    amount_msats: Mapped[int] = mapped_column(BigInteger)
    fee_msats: Mapped[int] = mapped_column(BigInteger, default=0)
    platform_fee_msats: Mapped[int] = mapped_column(BigInteger, default=0)

    # Destination
    destination_pubkey: Mapped[str | None] = mapped_column(String(66), nullable=True)

    # Status & routing
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.PENDING
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    route_hints_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    wallet: Mapped["Wallet"] = relationship(back_populates="payments")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Payment {self.payment_hash[:8]}... {self.amount_msats}msats [{self.status}]>"
