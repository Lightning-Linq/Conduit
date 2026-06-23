"""Unit tests for the read-only stablecoin quote service (Phase 1).

Injects a fake SwapProvider (no httpx, no network, no DB). Pins the graceful-fallback
contract: disabled / below-floor / token-unknown / provider-down all return a non-raising
`available=False` quote so the caller proceeds sats-only. The below-floor case must make
ZERO provider calls.
"""

import pytest

import conduit.services.stablecoin_quote as sq
from conduit.services.stablecoin_quote import get_stablecoin_quote
from conduit.services.swap_provider import SwapQuote, TokenInfo

_USDC_POL = "0xUSDC"


class FakeProvider:
    def __init__(self, *, tokens=None, fail=False):
        self.tokens_calls = 0
        self.quote_calls = 0
        self._tokens = tokens if tokens is not None else [TokenInfo("USDC", "137", _USDC_POL, 6)]
        self._fail = fail

    async def get_tokens(self):
        self.tokens_calls += 1
        return self._tokens

    async def get_quote(self, **kw):
        self.quote_calls += 1
        if self._fail:
            return None
        return SwapQuote(
            source_chain="Lightning",
            source_token="btc",
            target_chain=kw["target_chain"],
            target_token=kw["target_token"],
            source_amount_sats=kw["source_amount_sats"],
            target_amount_raw=1230412,
            target_decimals=kw["target_decimals"],
            exchange_rate="62000",
            protocol_fee_sats=5,
            network_fee_sats=5,
            min_amount_sats=335,
            max_amount_sats=2_000_000,
        )


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setattr(sq.settings, "stablecoin_quotes_enabled", True)
    monkeypatch.setattr(sq.settings, "stablecoin_min_floor_sats", 335)
    monkeypatch.setattr(sq.settings, "stablecoin_quote_cache_ttl_seconds", 30)
    sq._cache_clear()
    yield
    sq._cache_clear()


async def _q(amount, prov, **kw):
    return await get_stablecoin_quote(
        amount, target_chain="137", target_token=_USDC_POL, provider=prov, **kw
    )


async def test_disabled_returns_unavailable_without_calling_provider(monkeypatch):
    monkeypatch.setattr(sq.settings, "stablecoin_quotes_enabled", False)
    sq._cache_clear()
    prov = FakeProvider()
    res = await _q(2000, prov)
    assert res.available is False and res.reason == "disabled"
    assert prov.tokens_calls == 0 and prov.quote_calls == 0


async def test_below_floor_fails_gracefully_to_sats_no_api_call(enabled):
    """The headline: a sub-335-sat purchase gets NO stablecoin option and hits NO API."""
    prov = FakeProvider()
    res = await _q(100, prov)  # below the 335-sat floor
    assert res.available is False
    assert res.reason == "below_swap_minimum"
    assert res.min_sats == 335
    assert prov.tokens_calls == 0 and prov.quote_calls == 0  # never touched lendaswap


async def test_above_floor_returns_quote_with_correct_decimals(enabled):
    prov = FakeProvider()
    res = await _q(2000, prov)
    assert res.available is True and res.reason is None
    assert round(res.usd_estimate, 6) == 1.230412  # 6 decimals, not 8
    assert res.target_symbol == "USDC"
    assert res.protocol_fee_sats == 5
    assert res.min_sats == 335 and res.max_sats == 2_000_000


async def test_provider_down_fails_open(enabled):
    prov = FakeProvider(fail=True)
    res = await _q(2000, prov)
    assert res.available is False and res.reason == "unavailable"


async def test_unknown_token_unavailable(enabled):
    prov = FakeProvider(tokens=[TokenInfo("USDC", "999", "0xother", 6)])
    res = await _q(2000, prov)
    assert res.available is False and res.reason == "token_unknown"
    assert prov.quote_calls == 0  # didn't quote an unresolvable token


async def test_successful_quote_is_cached(enabled):
    prov = FakeProvider()
    a = await _q(2000, prov)
    b = await _q(2000, prov)
    assert a.available and b.available
    assert prov.quote_calls == 1  # second call served from cache
