"""Shared API dependencies — auth, LND client, database sessions."""

import hmac
import sys
from typing import Annotated

from fastapi import Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.config import settings
from conduit.core.database import async_session_factory
from conduit.services.lnd import LndClient


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
    if not expected or expected == "CHANGE-ME":
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

_lnd: LndClient | None = None


def get_lnd() -> LndClient:
    """Get or create the LND client connection."""
    global _lnd
    if _lnd is None or not _lnd.is_connected:
        _lnd = LndClient()
        _lnd.connect()
        print("[api] LND client connected", file=sys.stderr)
    return _lnd


# =============================================================================
# Database Session
# =============================================================================


async def get_session() -> AsyncSession:
    """Create a new async database session."""
    async with async_session_factory() as session:
        yield session
