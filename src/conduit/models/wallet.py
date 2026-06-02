"""Wallet model — each AI agent gets one."""


from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from conduit.models.base import Base


class Wallet(Base):
    """
    Represents an agent's wallet on the Conduit platform.

    Each wallet maps to a virtual balance tracked internally.
    Lightning channel liquidity is managed at the platform level.
    """

    __tablename__ = "wallets"

    # Owner identification (agent or user API key)
    owner_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Balance in millisatoshis (1 sat = 1000 msats)
    balance_msats: Mapped[int] = mapped_column(BigInteger, default=0)

    # Status
    is_active: Mapped[bool] = mapped_column(default=True)

    # Metadata
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    invoices: Mapped[list["Invoice"]] = relationship(  # noqa: F821
        back_populates="wallet", lazy="selectin"
    )
    payments: Mapped[list["Payment"]] = relationship(  # noqa: F821
        back_populates="wallet", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Wallet {self.id} owner={self.owner_id} balance={self.balance_msats}msats>"
