"""Shared API dependencies — auth, LND client, database sessions."""

import hmac
import sys
from typing import Annotated

from fastapi import Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.config import settings
from conduit.core.database import async_session_factory
from conduit.services.wallet_backend import WalletBackend


# =============================================================================
# Authentication
# =============================================================================


async def verify_api_key(
    x_api_key: Annotated[str, Header()],
) -> str:
    """Validate the X-API-Key header against the configured key.

    Uses a constant-time comparison so an attacker probing the API over
    the network can't recover the key one character at a time via
    response-timing differences.
    """
    expected = settings.conduit_api_key or ""
    # Reject the default placeholder explicitly — otherwise a misconfigured
    # server would accept "CHANGE-ME" as a valid key.
    if not expected or expected.startswith("CHANGE-ME"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server is not configured: CONDUIT_API_KEY is unset.",
        )
    provided = x_api_key or ""
    if not hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API key",
        )
    return x_api_key


# =============================================================================
# LND Client (lazy singleton)
# =============================================================================

_wallet: WalletBackend | None = None


def get_lnd() -> WalletBackend:
    """Get or create the wallet backend connection.

    Name kept as get_lnd() for backward compatibility — all existing
    callers use this function. The returned object implements
    WalletBackend and may be LND, NWC, or any future backend.
    """
    global _wallet
    if _wallet is None or not _wallet.is_connected:
        _wallet = _create_wallet_backend()
        _wallet.connect()
    return _wallet


def _create_wallet_backend() -> WalletBackend:
    """Factory: pick the right backend based on config."""
    backend = getattr(settings, "wallet_backend", "lnd").lower()

    # Auto-detect: if NWC connection string is set, use NWC
    nwc_uri = getattr(settings, "nwc_connection_string", "") or ""
    if backend == "auto":
        backend = "nwc" if nwc_uri else "lnd"

    if backend == "nwc":
        if not nwc_uri:
            raise RuntimeError(
                "WALLET_BACKEND=nwc but NWC_CONNECTION_STRING is not set. "
                "Paste your nostr+walletconnect:// URI in .env."
            )
        from conduit.services.nwc import NwcWalletBackend
        print("[api] Using NWC wallet backend", file=sys.stderr)
        return NwcWalletBackend(nwc_uri)

    # Default: LND
    from conduit.services.lnd import LndClient
    print("[api] Using LND wallet backend", file=sys.stderr)
    return LndClient()


# =============================================================================
# Database Session
# =============================================================================


async def get_session() -> AsyncSession:
    """Create a new async database session."""
    async with async_session_factory() as session:
        yield session
