"""Multi-relay NWC request behavior (reliability fix).

_async_nwc_request races every relay in the connection string and returns the first
wallet reply, so one slow/offline relay can't stall or fail the call when another is
healthy. The per-relay websocket flow (_request_on_relay) is mocked here; it's the
same code path exercised live against a real wallet.
"""

import secrets

import pytest

from conduit.services.nwc import NwcError, NwcWalletBackend, _derive_pubkey_from_secret

_SECRET = secrets.token_hex(32)
_WALLET = _derive_pubkey_from_secret(secrets.token_hex(32))
_R1 = "wss://relay-a.example.com"
_R2 = "wss://relay-b.example.com"
MULTI = f"nostr+walletconnect://{_WALLET}?relay={_R1}&relay={_R2}&secret={_SECRET}"
SINGLE = f"nostr+walletconnect://{_WALLET}?relay={_R1}&secret={_SECRET}"


def _backend(uri: str) -> NwcWalletBackend:
    c = NwcWalletBackend(uri)
    c.connect()
    return c


class TestMultiRelayRequest:
    async def test_falls_back_when_first_relay_is_silent(self):
        c = _backend(MULTI)
        tried = []

        async def fake(relay_url, event, event_id, method):
            tried.append(relay_url)
            return {"alias": "wallet"} if relay_url == _R2 else None  # only R2 answers

        c._request_on_relay = fake
        result = await c._async_nwc_request("get_info", {})
        assert result == {"alias": "wallet"}
        assert set(tried) == {_R1, _R2}  # both relays were raced

    async def test_no_response_from_any_relay_raises(self):
        c = _backend(MULTI)

        async def fake(relay_url, event, event_id, method):
            return None

        c._request_on_relay = fake
        with pytest.raises(NwcError, match="no response"):
            await c._async_nwc_request("get_info", {})

    async def test_wallet_error_response_propagates(self):
        c = _backend(MULTI)

        async def fake(relay_url, event, event_id, method):
            raise NwcError("NWC pay_invoice failed: [INSUFFICIENT_BALANCE] no funds")

        c._request_on_relay = fake
        with pytest.raises(NwcError, match="INSUFFICIENT_BALANCE"):
            await c._async_nwc_request("pay_invoice", {})

    async def test_single_relay_still_works(self):
        c = _backend(SINGLE)

        async def fake(relay_url, event, event_id, method):
            return {"balance": 42}

        c._request_on_relay = fake
        assert await c._async_nwc_request("get_balance", {}) == {"balance": 42}
