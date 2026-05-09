"""Conduit REST API — Lightning Payment Rails for AI Agents.

Mirrors all 23 MCP tools over HTTP with API key authentication.
Run alongside the MCP server for web, mobile, and remote agent access.

Usage:
    uvicorn conduit.main:app --host 0.0.0.0 --port 8000
    # or
    python -m conduit.main
"""

import sys
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from conduit import __version__
from conduit.core.config import settings
from conduit.api.deps import get_lnd
from conduit.api.routers import lightning, marketplace, security, nostr


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown — connect LND, initialize macaroons."""
    # Startup
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

    yield

    # Shutdown — nothing to clean up (gRPC channels close on GC)


app = FastAPI(
    title="Conduit API",
    description="Lightning Payment Rails for AI Agents — REST API",
    version=__version__,
    lifespan=lifespan,
)

# CORS — permissive in dev, lock down in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if not settings.is_production else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers — all under /api/v1
app.include_router(lightning.router, prefix="/api/v1")
app.include_router(marketplace.router, prefix="/api/v1")
app.include_router(security.router, prefix="/api/v1")
app.include_router(nostr.router, prefix="/api/v1")


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
