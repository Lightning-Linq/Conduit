"""Invoice model — Lightning invoices created for receiving payments."""

import enum
import uuid

from sqlalchemy import BigInteger, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from conduit.models.base import Base


class InvoiceStatus(str, enum.Enum):
    PENDING = "pending"
    SETTLED = "settled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class Invoice(Base):
    """
    A Lightning invoice (BOLT-11) created for an agent to receive payment.

    The invoice is generated via LND, and its lifecycle is tracked here.
    """

    __tablename__ = "invoices"

    # Belongs to a wallet
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id"), index=True
    )

    # Lightning invoice data
    payment_request: Mapped[str] = mapped_column(Text)  # The BOLT-11 encoded invoice
    payment_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    preimage: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Amount
    amount_msats: Mapped[int] = mapped_column(BigInteger)
    amount_paid_msats: Mapped[int] = mapped_column(BigInteger, default=0)

    # Metadata
    memo: Mapped[str | None] = mapped_column(String(512), nullable=True)
    expiry_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    status: Mapped[InvoiceStatus] = mapped_column(
        Enum(InvoiceStatus), default=InvoiceStatus.PENDING
    )

    # Relationships
    wallet: Mapped["Wallet"] = relationship(back_populates="invoices")  # noqa: F821

    def __repr__(self) -> str:
        return f"<Invoice {self.payment_hash[:8]}... {self.amount_msats}msats [{self.status}]>"
