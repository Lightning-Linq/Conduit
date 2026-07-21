"""Provider subscription model — the seller-side Pro tier.

One row per provider_name. `paid_until` in the future means the provider
holds an active Pro subscription (unlimited active listings); NULL or past
means free tier (`free_tier_max_active_skills` active listings). The
invoice_* columns track the single pending (unconfirmed) renewal invoice.

Non-custodial: the subscription is paid over Lightning to the platform
node (or the local wallet on self-hosted installs); Conduit stores only
payment proof, never funds.
"""

from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from conduit.models.base import Base


class ProviderSubscription(Base):
    __tablename__ = "provider_subscriptions"

    # Providers are keyed by name (matches Skill.provider_name / the H2
    # ownership checks). Unauthenticated — real enforcement is on the
    # LL-hosted node.
    provider_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)

    tier: Mapped[str] = mapped_column(String(20), default="pro", server_default="pro")
    paid_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # The one pending (unconfirmed) subscription invoice, if any.
    invoice_payment_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invoice_payment_request: Mapped[str | None] = mapped_column(Text, nullable=True)
    invoice_amount_sats: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    invoice_period: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # Which wallet issued it ("local" | "platform") — confirm verifies there.
    invoice_source: Mapped[str | None] = mapped_column(String(10), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<ProviderSubscription {self.provider_name} "
            f"tier={self.tier} paid_until={self.paid_until}>"
        )
