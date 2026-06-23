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


# CLI token aliases for the prefilled example (match the lendaswap example CLI).
_CLI_TOKEN_ALIAS = {"137": "usdc_pol", "42161": "usdc_arb", "1": "usdc_eth"}

_DISCLAIMER = (
    "Estimate only — the authoritative quote is computed in your own wallet at swap time. "
    "Verify every parameter before you sign. Conduit never moves your funds or runs the swap."
)


def build_quote_payload(
    amount_sats: int, quote: StablecoinQuote, *, invoice: str | None = None
) -> dict:
    """Shape a quote into an API/MCP payload with PREFILLED, NON-EXECUTING swap params for
    the user's OWN client. Conduit never executes the swap — these are instructions the
    user runs themselves. Param correctness is security-critical: a wrong amount, decimals,
    chain, or invoice would misdirect the user's real funds, so it's pinned by tests."""
    if not quote.available:
        messages = {
            "disabled": "Stablecoin quotes are off; pay in sats on Lightning.",
            "below_swap_minimum": (
                f"{amount_sats} sats is below the {quote.min_sats}-sat swap minimum — "
                "pay in sats on Lightning."
            ),
            "token_unknown": "That stablecoin/chain isn't supported; pay in sats.",
            "unavailable": "Stablecoin quotes are temporarily unavailable; pay in sats.",
        }
        return {
            "available": False,
            "reason": quote.reason,
            "pay_in_sats": True,
            "min_sats": quote.min_sats,
            "message": messages.get(quote.reason or "unavailable", "Pay in sats on Lightning."),
        }

    alias = _CLI_TOKEN_ALIAS.get(quote.target_chain or "", "usdc_pol")
    if invoice:
        # Payer funds a skill's sats invoice FROM stablecoin (EVM -> Lightning).
        prefilled = {
            "direction": "evm_to_lightning",
            "source_token": alias,
            "target": "btc_lightning",
            "target_invoice": invoice,  # echoed EXACTLY — the user verifies before signing
            "your_evm_address": "<YOUR_EVM_ADDRESS>",
            "cli_example": f"swap {alias} btc_lightning {invoice} <YOUR_EVM_ADDRESS>",
        }
    else:
        # Provider cashes sats OUT to stablecoin (Lightning -> EVM).
        prefilled = {
            "direction": "lightning_to_evm",
            "source": "btc_lightning",
            "source_amount_sats": int(amount_sats),
            "target_token": alias,
            "your_evm_address": "<YOUR_EVM_ADDRESS>",
            "cli_example": f"swap btc_lightning {alias} {int(amount_sats)} <YOUR_EVM_ADDRESS>",
        }

    return {
        "available": True,
        "binding": False,
        "disclaimer": _DISCLAIMER,
        "amount_sats": int(amount_sats),
        "estimate": {
            "target_symbol": quote.target_symbol,
            "target_chain": quote.target_chain,
            "target_token": quote.target_token,
            "target_amount": quote.usd_estimate,
            "protocol_fee_sats": quote.protocol_fee_sats,
            "network_fee_sats": quote.network_fee_sats,
            "min_sats": quote.min_sats,
            "max_sats": quote.max_sats,
        },
        "prefilled_swap": prefilled,
        "execute_in": "your own wallet/client (Conduit does not run the swap)",
    }


async def skill_price_usd_estimate(
    price_sats: int, *, provider: SwapProvider | None = None
) -> dict | None:
    """Advisory USD estimate of a skill's sats price for get_skill_details. Returns None
    when disabled / below floor / unavailable, so the detail view stays sats-first and
    never breaks."""
    quote = await get_stablecoin_quote(price_sats, provider=provider)
    if not quote.available:
        return None
    return {
        "symbol": quote.target_symbol,
        "chain": quote.target_chain,
        "amount": quote.usd_estimate,
        "binding": False,
        "note": "estimate only; pay in sats unless you opt into a swap in your own wallet",
    }
