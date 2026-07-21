"""Platform wallet — the NWC connection to Lightning Linq's platform node.

When `platform_fee_nwc_uri` is configured, platform fee invoices are issued
by (and settlement-verified against) LL's node instead of the local wallet,
making the fee actual platform revenue. Unset, callers fall back to the
local wallet (the historical behavior; coherent for self-hosted operators,
who then collect their own fees).

The URI embeds an NWC secret key: it lives in config/env only and must
never be logged, repr'd, or returned in any response.
"""

from __future__ import annotations

import sys

from conduit.core.config import settings
from conduit.services.nwc import NwcWalletBackend

_platform_wallet: NwcWalletBackend | None = None


def get_platform_wallet() -> NwcWalletBackend | None:
    """Return the platform NWC wallet, or None when unconfigured.

    Lazy singleton: constructed (cheap — URI parse + key derivation) on
    first use. A malformed URI degrades to None rather than raising, so a
    bad config can never take the execution path down with it.
    """
    global _platform_wallet
    uri = settings.platform_fee_nwc_uri
    if not uri:
        return None
    if _platform_wallet is None:
        try:
            wallet = NwcWalletBackend(uri)
            wallet.connect()
        except Exception as e:
            # Deliberately do not include the URI (it embeds the secret).
            print(f"[platform_wallet] invalid NWC config: {e}", file=sys.stderr)
            return None
        _platform_wallet = wallet
    return _platform_wallet


def reset_platform_wallet() -> None:
    """Drop the cached wallet (tests, config reload)."""
    global _platform_wallet
    _platform_wallet = None
