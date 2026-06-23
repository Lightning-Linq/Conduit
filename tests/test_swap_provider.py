"""Unit tests for the read-only, non-custodial swap-quote provider (Phase 1).

No DB, no network: httpx + the SSRF validator are monkeypatched. Pins the custody-adjacent
invariants — data minimization to the provider, decimals parsing (raw units, never assume 8),
SSRF refusal, and fail-open.
"""

import httpx

import conduit.services.swap_provider as sp
from conduit.services.swap_provider import LendaswapProvider
from conduit.services.url_safety import UnsafeURLError


class _FakeResp:
    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status_code = status
        self._payload = {} if payload is None else payload

    def json(self) -> dict:
        return self._payload


def _install(monkeypatch, *, resp=None, raises=None, validate_raises=False) -> list[dict]:
    """Monkeypatch the SSRF validator + httpx; return a list capturing outbound requests."""
    captured: list[dict] = []

    def fake_validate(url):
        if validate_raises:
            raise UnsafeURLError("blocked")
        return (url, "host", ["1.2.3.4"])

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            captured.append({"url": url, "params": params, "headers": headers})
            if raises is not None:
                raise raises
            return resp if resp is not None else _FakeResp()

    monkeypatch.setattr(sp, "resolve_and_validate", fake_validate)
    monkeypatch.setattr(sp.httpx, "AsyncClient", _FakeClient)
    return captured


_TOKENS = {
    "btc_tokens": [{"symbol": "BTC", "chain": "Lightning", "token_id": "btc", "decimals": 8}],
    "evm_tokens": [
        {"symbol": "USDC", "chain": "137", "token_id": "0xUSDC", "decimals": 6},
    ],
}
_QUOTE = {
    "exchange_rate": "62002.60",
    "target_amount": "1230412",
    "protocol_fee": 5,
    "network_fee": 5,
    "min_amount": 335,
    "max_amount": 2000000,
    "source_amount": "2000",
}


async def _quote(p, **kw):
    args = dict(
        source_chain="Lightning",
        source_token="btc",
        target_chain="137",
        target_token="0xUSDC",
        source_amount_sats=2000,
        target_decimals=6,
    )
    args.update(kw)
    return await p.get_quote(**args)


async def test_get_tokens_parses_decimals(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(payload=_TOKENS))
    tokens = await LendaswapProvider(base_url="https://api.example/").get_tokens()
    usdc = next(t for t in tokens if t.symbol == "USDC" and t.chain == "137")
    assert usdc.decimals == 6
    assert any(t.chain == "Lightning" and t.decimals == 8 for t in tokens)


async def test_get_quote_parses_raw_units_not_8_decimals(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(payload=_QUOTE))
    q = await _quote(LendaswapProvider(base_url="https://api.example/"))
    assert q is not None
    assert q.target_amount_raw == 1230412
    # The spike learning: 6 decimals -> 1.23 USDC, NOT 0.0123 (the CLI's 8-decimal bug).
    assert round(q.target_amount_decimal, 6) == 1.230412
    assert q.protocol_fee_sats == 5
    assert q.min_amount_sats == 335 and q.max_amount_sats == 2000000


async def test_quote_data_minimization(monkeypatch):
    captured = _install(monkeypatch, resp=_FakeResp(payload=_QUOTE))
    await _quote(LendaswapProvider(base_url="https://api.example/"))
    assert len(captured) == 1
    params = captured[0]["params"]
    expected = {"source_chain", "source_token", "target_chain", "target_token", "source_amount"}
    assert set(params) == expected
    blob = (str(params) + str(captured[0]["url"]) + str(captured[0]["headers"])).lower()
    for leak in ("payment_hash", "bolt11", "lnbc", "preimage", "pubkey", "payer", "macaroon"):
        assert leak not in blob


async def test_ssrf_guard_refuses_and_makes_no_call(monkeypatch):
    captured = _install(monkeypatch, validate_raises=True)
    p = LendaswapProvider(base_url="http://169.254.169.254/")
    assert await p.get_tokens() == []
    assert await _quote(p) is None
    assert captured == []  # never hit the network


async def test_fail_open_on_http_error(monkeypatch):
    _install(monkeypatch, raises=httpx.ConnectError("down"))
    p = LendaswapProvider(base_url="https://api.example/")
    assert await p.get_tokens() == []
    assert await _quote(p) is None


async def test_non_200_returns_none(monkeypatch):
    _install(monkeypatch, resp=_FakeResp(status=503, payload={"error": "x"}))
    p = LendaswapProvider(base_url="https://api.example/")
    assert await p.get_tokens() == []
    assert await _quote(p) is None
