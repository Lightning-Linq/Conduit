"""Read-only, non-custodial stablecoin swap quotes (Phase 1, lendaswap).

CUSTODY INVARIANT (load-bearing — do not weaken): this module is READ-ONLY. It calls
only `GET /tokens` and `GET /quote` on the lendaswap API. It NEVER creates or funds a
swap, NEVER holds a key/seed/mnemonic, and NEVER moves funds. The swap and all signing
happen in the user's own client. See
`.claude/.plans/2026-06-23-stablecoin-phase1-lendaswap.md`.

DATA MINIMIZATION (load-bearing): the `/quote` call sends ONLY amount + token/chain. It
MUST NOT send `payment_hash`, a BOLT11 invoice, or any payer identity to the provider.

The outbound URL is SSRF-guarded via `url_safety` (same as every other outbound call),
and every method FAILS OPEN — any error returns empty/None so discovery and settlement
never break when lendaswap is unreachable.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from conduit.core.config import settings
from conduit.services.url_safety import UnsafeURLError, resolve_and_validate

_TIMEOUT_SECONDS = 8.0


@dataclass(frozen=True)
class TokenInfo:
    """A swappable token as advertised by the provider's /tokens endpoint."""

    symbol: str
    chain: str  # "Lightning" | "Arkade" | "Bitcoin" | EVM chain id ("137", "42161", "1")
    token_id: str
    decimals: int


@dataclass(frozen=True)
class SwapQuote:
    """A read-only swap quote. Amounts in the target token are RAW smallest units;
    use `target_amount_decimal` (raw / 10**decimals) for display — never assume 8."""

    source_chain: str
    source_token: str
    target_chain: str
    target_token: str
    source_amount_sats: int
    target_amount_raw: int
    target_decimals: int
    exchange_rate: str
    protocol_fee_sats: int
    network_fee_sats: int
    min_amount_sats: int
    max_amount_sats: int

    @property
    def target_amount_decimal(self) -> float:
        return self.target_amount_raw / (10**self.target_decimals)


class SwapProvider(Protocol):
    """Read-only swap-quote provider (mirrors the WalletBackend protocol pattern)."""

    async def get_tokens(self) -> list[TokenInfo]: ...

    async def get_quote(
        self,
        *,
        source_chain: str,
        source_token: str,
        target_chain: str,
        target_token: str,
        source_amount_sats: int,
        target_decimals: int,
    ) -> SwapQuote | None: ...


class LendaswapProvider:
    """Read-only lendaswap client. SSRF-guarded; fail-open (returns []/None on any error)."""

    def __init__(self, base_url: str | None = None, org_code: str | None = None) -> None:
        self._base_url = (base_url or settings.lendaswap_api_url).rstrip("/")
        self._org_code = org_code if org_code is not None else settings.lendaswap_org_code

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._org_code:
            # Attribution only; not a fee mechanism (lendaswap has none).
            headers["X-Org-Code"] = self._org_code
        return headers

    async def get_tokens(self) -> list[TokenInfo]:
        data = await self._get("/tokens", params=None)
        if not isinstance(data, dict):
            return []
        tokens: list[TokenInfo] = []
        for t in data.get("btc_tokens") or []:
            tokens.append(TokenInfo(
                symbol=t["symbol"], chain=str(t["chain"]),
                token_id=str(t["token_id"]), decimals=int(t.get("decimals", 8)),
            ))
        for t in data.get("evm_tokens") or []:
            tokens.append(TokenInfo(
                symbol=t["symbol"], chain=str(t["chain"]),
                token_id=str(t["token_id"]), decimals=int(t["decimals"]),
            ))
        return tokens

    async def get_quote(
        self,
        *,
        source_chain: str,
        source_token: str,
        target_chain: str,
        target_token: str,
        source_amount_sats: int,
        target_decimals: int,
    ) -> SwapQuote | None:
        # DATA MINIMIZATION: only amount + token/chain. No payment_hash / BOLT11 / identity.
        params = {
            "source_chain": source_chain,
            "source_token": source_token,
            "target_chain": target_chain,
            "target_token": target_token,
            "source_amount": str(int(source_amount_sats)),
        }
        data = await self._get("/quote", params=params)
        if not isinstance(data, dict) or "target_amount" not in data:
            return None
        try:
            return SwapQuote(
                source_chain=source_chain,
                source_token=source_token,
                target_chain=target_chain,
                target_token=target_token,
                source_amount_sats=int(source_amount_sats),
                target_amount_raw=int(data["target_amount"]),
                target_decimals=int(target_decimals),
                exchange_rate=str(data.get("exchange_rate", "")),
                protocol_fee_sats=int(data.get("protocol_fee", 0)),
                network_fee_sats=int(data.get("network_fee", 0)),
                min_amount_sats=int(data.get("min_amount", 0)),
                max_amount_sats=int(data.get("max_amount", 0)),
            )
        except (TypeError, ValueError):
            return None

    async def _get(self, path: str, params: dict[str, str] | None) -> Any:
        url = f"{self._base_url}{path}"
        # SSRF guard: refuse to call anything that resolves to internal space.
        try:
            validated_url, _host, _ips = resolve_and_validate(url)
        except UnsafeURLError:
            return None
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, follow_redirects=False
            ) as client:
                resp = await client.get(validated_url, params=params, headers=self._headers())
                if resp.status_code != 200:
                    return None
                return resp.json()
        except (httpx.HTTPError, ValueError):
            # Fail-open: never break a caller because the quote provider is down.
            return None
