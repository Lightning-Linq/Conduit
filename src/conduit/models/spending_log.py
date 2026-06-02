"""Spending log model — tracks every outgoing payment for limit enforcement."""

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from conduit.models.base import Base


class SpendingLog(Base):
    """
    Records every outgoing payment attempt (successful or blocked).

    Used by the spending limiter to enforce per-payment, hourly, and
    daily caps. Also provides an audit trail of all spending activity.
    """

    __tablename__ = "spending_logs"

    # What triggered this spend
    # e.g. "pay_invoice", "request_skill_execution"
    tool_name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Amount
    amount_sats: Mapped[int] = mapped_column(BigInteger)

    # Outcome
    status: Mapped[str] = mapped_column(String(20))  # "allowed", "blocked", "confirmed"
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Payment reference (links to the actual LND payment if allowed)
    payment_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<SpendingLog {self.tool_name} {self.amount_sats} sats [{self.status}]>"
