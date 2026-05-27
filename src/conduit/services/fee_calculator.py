"""Platform fee calculation for skill executions.

Two-invoice model (Option A): consumer pays the provider directly for the
skill price, and pays a separate invoice to the platform node for the fee.
Fully non-custodial -- Conduit never holds provider funds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from conduit.core.config import settings


@dataclass(frozen=True)
class FeeBreakdown:
    """Breakdown of a skill execution payment."""

    skill_price_sats: int       # What the provider charges
    platform_fee_sats: int      # What the platform charges (separate invoice)
    total_consumer_cost_sats: int  # skill_price + platform_fee
    fee_percent: float          # The rate applied

    @property
    def fee_enabled(self) -> bool:
        return self.platform_fee_sats > 0


def calculate_fee(skill_price_sats: int) -> FeeBreakdown:
    """
    Calculate the platform fee for a skill execution.

    Rules:
    - If fees are disabled in config, fee is 0.
    - If skill is free (0 sats), no fee.
    - Fee = ceil(price * percent / 100), floored at minimum_sats.
    - Uses ceiling to avoid rounding down to 0 on small amounts.

    Returns a FeeBreakdown with the full cost split.
    """
    if not settings.platform_fee_enabled or skill_price_sats <= 0:
        return FeeBreakdown(
            skill_price_sats=skill_price_sats,
            platform_fee_sats=0,
            total_consumer_cost_sats=skill_price_sats,
            fee_percent=0.0,
        )

    raw_fee = skill_price_sats * settings.transaction_fee_percent / 100
    fee_sats = max(
        math.ceil(raw_fee),
        settings.platform_fee_minimum_sats,
    )

    return FeeBreakdown(
        skill_price_sats=skill_price_sats,
        platform_fee_sats=fee_sats,
        total_consumer_cost_sats=skill_price_sats + fee_sats,
        fee_percent=settings.transaction_fee_percent,
    )
