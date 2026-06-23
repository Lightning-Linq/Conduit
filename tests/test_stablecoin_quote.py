"""Unit tests for the read-only stablecoin quote service (Phase 1).

Injects a fake SwapProvider (no httpx, no network, no DB). Pins the graceful-fallback
contract: disabled / below-floor / token-unknown / provider-down all return a non-raising
`available=False` quote so the caller proceeds sats-only. The below-floor case must make
ZERO provider calls.
"""

import pytest

import conduit.services.stablecoin_quote as sq
from conduit.services.stablecoin_quote import (
    DEFAULT_TARGET_TOKEN,
    StablecoinQuote,
    build_quote_payload,
    get_stablecoin_quote,
    skill_price_usd_estimate,
)
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


# --- build_quote_payload: prefilled-param correctness (the elevated security surface) ---

_AVAIL = StablecoinQuote(
    available=True,
    usd_estimate=1.230412,
    target_symbol="USDC",
    target_chain="137",
    target_token="0xUSDC",
    protocol_fee_sats=5,
    network_fee_sats=5,
    min_sats=335,
    max_sats=2_000_000,
)


def test_payload_below_floor_offers_no_swap_and_says_pay_in_sats():
    q = StablecoinQuote(available=False, reason="below_swap_minimum", min_sats=335)
    p = build_quote_payload(100, q)
    assert p["available"] is False and p["pay_in_sats"] is True
    assert "335" in p["message"] and "sats" in p["message"].lower()
    assert "prefilled_swap" not in p  # no swap params below the floor


def test_payload_payer_funds_invoice_echoes_bolt11_exactly():
    invoice = "lnbc20u1pFAKEINVOICEexampleonly"
    p = build_quote_payload(2000, _AVAIL, invoice=invoice)
    pf = p["prefilled_swap"]
    assert pf["direction"] == "evm_to_lightning"
    assert pf["target_invoice"] == invoice  # EXACT echo — wrong here misdirects funds
    assert invoice in pf["cli_example"]
    assert p["binding"] is False
    assert "verify" in p["disclaimer"].lower()


def test_payload_provider_cashout_amount_and_token_exact():
    p = build_quote_payload(2000, _AVAIL)
    pf = p["prefilled_swap"]
    assert pf["direction"] == "lightning_to_evm"
    assert pf["source_amount_sats"] == 2000
    assert "2000" in pf["cli_example"] and "usdc_pol" in pf["cli_example"]
    assert p["estimate"]["target_amount"] == 1.230412
    assert p["estimate"]["target_chain"] == "137"


def test_payload_never_leaks_secrets_or_executes():
    p = build_quote_payload(2000, _AVAIL, invoice="lnbc1example")
    blob = str(p).lower()
    for bad in ("mnemonic", "seed", "private_key", "createswap", "fund_swap", "preimage"):
        assert bad not in blob


# --- skill_price_usd_estimate: gated, graceful (Task 4 helper) ---


def _default_provider():
    return FakeProvider(tokens=[TokenInfo("USDC", "137", DEFAULT_TARGET_TOKEN, 6)])


async def test_skill_estimate_none_below_floor(enabled):
    assert await skill_price_usd_estimate(100, provider=_default_provider()) is None


async def test_skill_estimate_available(enabled):
    est = await skill_price_usd_estimate(2000, provider=_default_provider())
    assert est is not None
    assert est["symbol"] == "USDC"
    assert round(est["amount"], 6) == 1.230412
    assert est["binding"] is False


async def test_skill_estimate_none_when_disabled(monkeypatch):
    monkeypatch.setattr(sq.settings, "stablecoin_quotes_enabled", False)
    sq._cache_clear()
    assert await skill_price_usd_estimate(2000, provider=_default_provider()) is None
