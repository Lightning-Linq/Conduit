"""Read-only stablecoin quote service (Phase 1).

Wraps a SwapProvider into one advisory quote for display + (later) prefilled params.

GRACEFUL FALLBACK (load-bearing): this NEVER raises for the normal "can't / shouldn't
swap" cases. When stablecoin is disabled, the amount is below the swap floor, the token
is unknown, or the provider is unreachable, it returns an `available=False` StablecoinQuote
so the caller silently proceeds with **sats-only on Lightning**. The per-call sats purchase
is always the default and is never affected by this module — sub-floor purchases simply are
never offered a stablecoin option (and don't even hit the lendaswap API).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from conduit.core.config import settings
from conduit.services.swap_provider import LendaswapProvider, SwapProvider

# Default target: USDC on Polygon (cheapest, most-liquid path in the verified spike).
DEFAULT_TARGET_CHAIN = "137"
DEFAULT_TARGET_TOKEN = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"

_SOURCE_CHAIN = "Lightning"
_SOURCE_TOKEN = "btc"


@dataclass(frozen=True)
class StablecoinQuote:
    """Advisory, non-binding quote. `available=False` means: proceed sats-only."""

    available: bool
    # None | "disabled" | "below_swap_minimum" | "token_unknown" | "unavailable"
    reason: str | None = None
    usd_estimate: float | None = None  # target amount in token decimal units (e.g. 1.23)
    target_symbol: str | None = None
    target_chain: str | None = None
    target_token: str | None = None
    protocol_fee_sats: int | None = None
    network_fee_sats: int | None = None
    min_sats: int | None = None
    max_sats: int | None = None


# Cache only successful quotes (so a transient "unavailable" recovers immediately).
_cache: dict[tuple[int, str, str], tuple[float, StablecoinQuote]] = {}


def _cache_clear() -> None:
    """Test hook."""
    _cache.clear()


async def get_stablecoin_quote(
    amount_sats: int,
    *,
    target_chain: str = DEFAULT_TARGET_CHAIN,
    target_token: str = DEFAULT_TARGET_TOKEN,
    provider: SwapProvider | None = None,
) -> StablecoinQuote:
    # 1. Feature gate — opt-in, default off.
    if not settings.stablecoin_quotes_enabled:
        return StablecoinQuote(available=False, reason="disabled")

    # 2. Floor pre-check — below the swap minimum: NO API call, fall back to sats-only.
    floor = settings.stablecoin_min_floor_sats
    if amount_sats < floor:
        return StablecoinQuote(available=False, reason="below_swap_minimum", min_sats=floor)

    # 3. Cache hit (successful quotes only).
    key = (int(amount_sats), target_chain, target_token)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < settings.stablecoin_quote_cache_ttl_seconds:
        return hit[1]

    prov = provider or LendaswapProvider()

    # 4. Resolve target token decimals from /tokens (authoritative; never assume 8).
    match = None
    for t in await prov.get_tokens():
        if t.chain == target_chain and t.token_id.lower() == target_token.lower():
            match = t
            break
    if match is None:
        return StablecoinQuote(available=False, reason="token_unknown")

    # 5. Quote (fail-open).
    q = await prov.get_quote(
        source_chain=_SOURCE_CHAIN,
        source_token=_SOURCE_TOKEN,
        target_chain=target_chain,
        target_token=target_token,
        source_amount_sats=amount_sats,
        target_decimals=match.decimals,
    )
    if q is None:
        return StablecoinQuote(available=False, reason="unavailable")

    # 6. Belt-and-suspenders: honor the live min from the quote (the floor may drift).
    if q.min_amount_sats and amount_sats < q.min_amount_sats:
        return StablecoinQuote(
            available=False, reason="below_swap_minimum", min_sats=q.min_amount_sats
        )

    result = StablecoinQuote(
        available=True,
        usd_estimate=q.target_amount_decimal,
        target_symbol=match.symbol,
        target_chain=target_chain,
        target_token=target_token,
        protocol_fee_sats=q.protocol_fee_sats,
        network_fee_sats=q.network_fee_sats,
        min_sats=q.min_amount_sats,
        max_sats=q.max_amount_sats,
    )
    _cache[key] = (now, result)
    return result
