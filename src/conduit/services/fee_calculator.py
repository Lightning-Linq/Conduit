"""Platform fee calculation for skill executions.

Fee-inclusive, seller-pays model over two-invoice rails (non-custodial):
the buyer pays exactly the listed price. The platform fee is carved OUT of
that price — the provider's invoice is for price - fee, and a separate fee
invoice covers the rest. Conduit never holds provider funds.

Prices below the waive floor carry no fee (the fee would eat a dust-priced
skill), so the provider always nets at least 1 sat on any fee-bearing sale.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from conduit.core.config import settings


@dataclass(frozen=True)
class FeeBreakdown:
    """Breakdown of a skill execution payment."""

    skill_price_sats: int          # The listed price — exactly what the buyer pays
    platform_fee_sats: int         # Carved out of the price (separate invoice)
    provider_amount_sats: int      # What the provider nets: price - fee
    total_consumer_cost_sats: int  # == skill_price_sats (fee-inclusive)
    fee_percent: float             # The rate applied

    @property
    def fee_enabled(self) -> bool:
        return self.platform_fee_sats > 0


def calculate_fee(skill_price_sats: int) -> FeeBreakdown:
    """
    Calculate the platform fee split for a skill execution.

    Rules:
    - If fees are disabled in config, fee is 0 and the provider gets the full price.
    - If the skill is free (0 sats) or priced below the waive floor, no fee.
    - Fee = ceil(price * percent / 100), floored at minimum_sats, taken FROM the price.
    - Invariant: provider_amount + platform_fee == price; buyer total == price.

    Returns a FeeBreakdown with the full split.
    """
    price = max(skill_price_sats, 0)
    no_fee = (
        not settings.platform_fee_enabled
        or price <= 0
        or price < settings.platform_fee_waive_below_sats
    )
    if no_fee:
        return FeeBreakdown(
            skill_price_sats=skill_price_sats,
            platform_fee_sats=0,
            provider_amount_sats=price,
            total_consumer_cost_sats=price,
            fee_percent=0.0,
        )

    raw_fee = price * settings.transaction_fee_percent / 100
    fee_sats = max(
        math.ceil(raw_fee),
        settings.platform_fee_minimum_sats,
    )
    # The fee is carved out of the price; never let it zero out the provider.
    fee_sats = min(fee_sats, price - 1)

    return FeeBreakdown(
        skill_price_sats=skill_price_sats,
        platform_fee_sats=fee_sats,
        provider_amount_sats=price - fee_sats,
        total_consumer_cost_sats=price,
        fee_percent=settings.transaction_fee_percent,
    )
