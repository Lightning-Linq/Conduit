"""Platform wallet accessor — the NWC connection to LL's platform node.

The platform fee invoice is issued by this wallet when configured; unset
config means fee invoices stay on the local wallet (current behavior).
The NWC URI embeds a secret key: it must never appear in logs or errors.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from conduit.services import platform_wallet

# Valid-shape NWC URI: 64-hex wallet pubkey + relay + 64-hex secret (test values).
FAKE_URI = (
    "nostr+walletconnect://" + "ab" * 32 + "?relay=wss://relay.example.com&secret=" + "cd" * 32
)


@pytest.fixture(autouse=True)
def _reset():
    platform_wallet.reset_platform_wallet()
    yield
    platform_wallet.reset_platform_wallet()


def test_unconfigured_returns_none():
    with patch.object(platform_wallet.settings, "platform_fee_nwc_uri", ""):
        assert platform_wallet.get_platform_wallet() is None


def test_configured_returns_nwc_wallet_singleton():
    with patch.object(platform_wallet.settings, "platform_fee_nwc_uri", FAKE_URI):
        w1 = platform_wallet.get_platform_wallet()
        w2 = platform_wallet.get_platform_wallet()
    assert w1 is not None
    assert w1 is w2  # lazy singleton — one connection object per process
    # It's a real NwcWalletBackend pointed at the platform node's pubkey.
    assert w1._conn.wallet_pubkey == "ab" * 32


def test_bad_uri_returns_none_not_raise():
    """A malformed URI must degrade to 'unconfigured', never crash callers."""
    with patch.object(platform_wallet.settings, "platform_fee_nwc_uri", "not-a-uri"):
        assert platform_wallet.get_platform_wallet() is None


def test_secret_not_in_repr():
    with patch.object(platform_wallet.settings, "platform_fee_nwc_uri", FAKE_URI):
        w = platform_wallet.get_platform_wallet()
    assert "cd" * 32 not in repr(w)
    assert "cd" * 32 not in str(w)
