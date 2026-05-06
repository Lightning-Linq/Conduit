"""Skill model — services that agents offer on the marketplace."""

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from conduit.models.base import Base


class Skill(Base):
    """
    A skill (service) that an AI agent offers on the marketplace.

    Non-custodial design: the skill includes the provider's Lightning
    address and node pubkey. Payments go directly from consumer to
    provider — Conduit never touches the sats.
    """

    __tablename__ = "skills"

    # Provider identity
    provider_name: Mapped[str] = mapped_column(String(255))
    provider_pubkey: Mapped[str | None] = mapped_column(String(66), nullable=True)
    provider_lightning_address: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )

    # Skill details
    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[str] = mapped_column(Text)
    category: Mapped[str] = mapped_column(String(100), index=True)
    tags: Mapped[str | None] = mapped_column(Text, nullable=True)  # comma-separated

    # Pricing (in satoshis)
    price_sats: Mapped[int] = mapped_column(BigInteger)

    # Input/output schemas (JSON Schema format)
    input_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output_schema: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Endpoint for execution (where the skill actually runs)
    endpoint_url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Stats (updated after executions)
    total_executions: Mapped[int] = mapped_column(Integer, default=0)
    avg_rating: Mapped[float] = mapped_column(Numeric(3, 2), default=0.0)
    avg_response_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Relationships
    executions: Mapped[list["SkillExecution"]] = relationship(
        back_populates="skill", lazy="selectin"
    )

    def __repr__(self) -> str:
        return f"<Skill {self.name} by {self.provider_name} @ {self.price_sats} sats>"
