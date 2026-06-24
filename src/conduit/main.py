"""Conduit REST API — Lightning Payment Rails for AI Agents.

Mirrors all 23 MCP tools over HTTP with API key and L402 authentication.
Run alongside the MCP server for web, mobile, and remote agent access.

Usage:
    uvicorn conduit.main:app --host 0.0.0.0 --port 8000
    # or
    python -m conduit.main
"""

import asyncio
import stat
import sys
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from conduit import __version__
from conduit.api.deps import get_lnd
from conduit.api.middleware.l402 import L402Middleware
from conduit.api.middleware.rate_limit import RateLimitMiddleware
from conduit.api.middleware.verification import VerificationEnforcementMiddleware
from conduit.api.routers import admin, federation, lightning, marketplace, nostr, security
from conduit.core.config import settings


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
        msg = (
            "FATAL: secret files are world/group accessible. "
            "Fix with: chmod 600 <file>\n  " + "\n  ".join(bad)
        )
        print(msg, file=sys.stderr)
        # M9: Always exit — the fix (chmod 600) is trivial and dev
        # environments commonly run with prod-like credentials.
        sys.exit(1)


async def _federation_refresh_loop() -> None:
    """Periodically pull relays + peers into the cache (Federation #2).

    Sleep-first so startup never blocks on the network/DB; resilient (a failed
    cycle is logged and the loop continues); cancelled on shutdown.
    """
    from conduit.core.database import async_session_factory
    from conduit.services.federation_cache import refresh_all_cached
    from conduit.services.federation_catalog import refresh_catalog

    interval = max(60, settings.federation_refresh_interval_minutes * 60)
    while True:
        await asyncio.sleep(interval)
        try:
            async with async_session_factory() as session:
                n = await refresh_all_cached(
                    session,
                    relay_urls=settings.nostr_relay_list,
                    peer_urls=settings.federation_peer_list,
                )
                skills = await refresh_catalog(
                    session,
                    relay_urls=settings.nostr_relay_list,
                    peer_urls=settings.federation_peer_list,
                )
                await session.commit()
            if n or skills:
                print(
                    f"[federation] background refresh cached {n} attestations, {skills} skills",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"[federation] background refresh failed: {e}", file=sys.stderr)


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

    # L402 status — NEW-M3: fail fast if secret is missing in production
    if settings.l402_enabled:
        from conduit.services.l402 import _get_l402_secret
        _get_l402_secret()  # raises RuntimeError if placeholder in production
        print(
            f"[api] L402 enabled — default price: {settings.l402_default_price_sats} sats, "
            f"token TTL: {settings.l402_token_expiry_seconds}s",
            file=sys.stderr,
        )
    else:
        print("[api] L402 disabled — API key auth only", file=sys.stderr)

    # Federation #2: background relay/peer refresh (sleep-first; cancelled below).
    fed_task = None
    if settings.federation_enabled:
        fed_task = asyncio.create_task(_federation_refresh_loop())
        print("[api] Federation refresh loop started", file=sys.stderr)

    yield

    if fed_task is not None:
        fed_task.cancel()

    # L3: Explicitly close the LND gRPC channel on shutdown
    try:
        lnd = get_lnd()
        if hasattr(lnd, "disconnect"):
            lnd.disconnect()
        print("[api] LND connection closed", file=sys.stderr)
    except Exception:
        pass


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
from conduit.core.database import async_session_factory  # noqa: E402

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
app.include_router(federation.router, prefix="/api/v1")


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


def run() -> None:
    """Console-script entry point (conduit-api): serve the REST API with uvicorn."""
    import uvicorn

    # N10: enforce here too, so the CLI / console-script path fails fast even if an
    # ASGI server is configured without the lifespan (where the check otherwise runs).
    _check_secret_file_permissions()
    uvicorn.run(
        "conduit.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
    )


if __name__ == "__main__":
    run()
