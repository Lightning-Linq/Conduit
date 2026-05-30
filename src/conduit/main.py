"""Conduit REST API — Lightning Payment Rails for AI Agents.

Mirrors all 23 MCP tools over HTTP with API key and L402 authentication.
Run alongside the MCP server for web, mobile, and remote agent access.

Usage:
    uvicorn conduit.main:app --host 0.0.0.0 --port 8000
    # or
    python -m conduit.main
"""

import os
import stat
import sys
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from conduit import __version__
from conduit.core.config import settings
from conduit.api.deps import get_lnd
from conduit.api.middleware.l402 import L402Middleware
from conduit.api.middleware.rate_limit import RateLimitMiddleware
from conduit.api.middleware.verification import VerificationEnforcementMiddleware
from conduit.api.routers import admin, lightning, marketplace, security, nostr


def _check_secret_file_permissions() -> None:
    """
    Refuse to start if .env or LND credentials are world/group readable.
    These files contain (a) the API key that gates the whole REST surface
    and (b) the admin macaroon — anyone who can read them controls the
    node.
    """
    project_root = Path(__file__).resolve().parent.parent.parent
    paths = [
        project_root / ".env",
        settings.lnd_macaroon_path,
        settings.lnd_tls_cert_path,
    ]
    creds_dir = project_root / "credentials"
    if creds_dir.is_dir():
        paths.extend(creds_dir.iterdir())

    bad: list[str] = []
    for p in paths:
        try:
            p = Path(p).expanduser()
            if not p.exists() or not p.is_file():
                continue
            mode = p.stat().st_mode
            # Any group/other permission bits set is too permissive.
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                bad.append(f"{p}  mode={oct(mode & 0o777)}")
        except Exception:
            continue

    if bad:
        print(
            "FATAL: secret files are world/group accessible. "
            "Fix with: chmod 600 <file>\n  " + "\n  ".join(bad),
            file=sys.stderr,
        )
        # In production we exit. In dev we warn loudly so tests still pass.
        if settings.is_production:
            sys.exit(1)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown — connect LND, initialize macaroons."""
    # Startup
    _check_secret_file_permissions()

    try:
        lnd = get_lnd()
        info = lnd.get_info()
        print(f"[api] Connected to LND node: {info.alias} ({info.pubkey[:16]}...)", file=sys.stderr)
    except Exception as e:
        print(f"[api] Warning: Could not connect to LND: {e}", file=sys.stderr)
        print("[api] Lightning endpoints will fail until LND is available", file=sys.stderr)

    # Initialize macaroon session
    from conduit.services.macaroon_auth import initialize_root_session
    initialize_root_session()
    print("[api] Root macaroon initialized", file=sys.stderr)

    # L402 status
    if settings.l402_enabled:
        print(
            f"[api] L402 enabled — default price: {settings.l402_default_price_sats} sats, "
            f"token TTL: {settings.l402_token_expiry_seconds}s",
            file=sys.stderr,
        )
    else:
        print("[api] L402 disabled — API key auth only", file=sys.stderr)

    yield

    # Shutdown — nothing to clean up (gRPC channels close on GC)


app = FastAPI(
    title="Conduit API",
    description="Lightning Payment Rails for AI Agents — REST API",
    version=__version__,
    lifespan=lifespan,
)

# CORS — strict by default. Operators who need cross-origin access must
# set CORS_ALLOW_ORIGINS explicitly to a comma-separated list of origins.
# We never combine wildcard origins with credentials (browsers reject the
# combination anyway; we reject it server-side for clarity).
_cors_origins = settings.cors_origin_list
_allow_credentials = True
if "*" in _cors_origins:
    if settings.is_production:
        raise RuntimeError(
            "CORS_ALLOW_ORIGINS=* is not allowed in production. "
            "Set an explicit comma-separated origin list."
        )
    # Wildcard requires allow_credentials=False per the CORS spec.
    _allow_credentials = False

# H10: DELETE is intentionally excluded from allow_methods. Admin and
# delete endpoints are server-to-server only — browser clients cannot
# issue cross-origin DELETE requests. This is a security feature, not
# a bug. Do NOT add "DELETE" here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-API-Key", "Authorization"],
)

# Rate limiting middleware — maps routes to tool names and enforces
# per-tool sliding-window limits. Runs before auth so rate-limited
# requests are rejected cheaply (no LND/DB round-trips).
app.add_middleware(RateLimitMiddleware)

# L402 middleware — sits between CORS and routing. Only active when
# L402_ENABLED=true in .env; otherwise passes everything through.
app.add_middleware(L402Middleware, get_lnd_fn=get_lnd)

# Verification enforcement — warns or blocks execution of unverified skills.
# Uses async DB session to look up skill verification status.
from conduit.core.database import async_session_factory
app.add_middleware(
    VerificationEnforcementMiddleware,
    get_session_fn=async_session_factory,
)

# Mount routers — all under /api/v1
app.include_router(lightning.router, prefix="/api/v1")
app.include_router(marketplace.router, prefix="/api/v1")
app.include_router(security.router, prefix="/api/v1")
app.include_router(nostr.router, prefix="/api/v1")
app.include_router(admin.router, prefix="/api/v1")


@app.get("/health")
async def health_check() -> dict:
    """Health check — confirms the API is running."""
    return {
        "status": "healthy",
        "version": __version__,
        "service": "Conduit API",
    }


@app.get("/")
async def root() -> dict:
    """API root — service info and endpoint map."""
    return {
        "service": "Conduit",
        "version": __version__,
        "docs": "/docs",
        "l402_enabled": settings.l402_enabled,
        "endpoints": {
            "lightning": "/api/v1/lightning",
            "marketplace": "/api/v1/marketplace",
            "security": "/api/v1/security",
            "nostr": "/api/v1/nostr",
        },
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "conduit.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
    )
