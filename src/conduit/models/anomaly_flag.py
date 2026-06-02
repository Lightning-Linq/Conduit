"""Anomaly flag model — tracks suspicious transaction patterns."""

from sqlalchemy import BigInteger, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from conduit.models.base import Base


class AnomalyFlag(Base):
    """
    Records a suspicious pattern detected in payment or execution activity.

    Flags are advisory (warning mode) — they don't block transactions
    but create an audit trail for review. Severity levels:
    - low: unusual but possibly legitimate (e.g. same skill purchased twice quickly)
    - medium: likely suspicious (e.g. repeated same-amount payments in short window)
    - high: strong indicator of abuse (e.g. circular payment detected)
    """

    __tablename__ = "anomaly_flags"

    # What type of anomaly
    flag_type: Mapped[str] = mapped_column(String(50))
    # Types: "circular_payment", "rapid_repeat", "structuring", "volume_spike", "self_payment"

    severity: Mapped[str] = mapped_column(String(10))  # "low", "medium", "high"

    # What triggered it
    description: Mapped[str] = mapped_column(Text)

    # Related entities
    payment_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    consumer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    provider_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    amount_sats: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # Whether it's been reviewed
    reviewed: Mapped[bool] = mapped_column(default=False)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<AnomalyFlag {self.flag_type} [{self.severity}] {self.description[:50]}>"
