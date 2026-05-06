"""Conduit FastAPI application entry point."""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from conduit import __version__
from conduit.core.config import settings
from conduit.services.lnd import lnd_client
from conduit.api.routers import wallets, invoices, payments


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup/shutdown lifecycle — connect to LND, warm up connections."""
    # Startup
    try:
        await lnd_client.connect()
        print(f"Connected to LND at {settings.lnd_host}:{settings.lnd_grpc_port}")
    except Exception as e:
        print(f"Warning: Could not connect to LND: {e}")
        print("Running in degraded mode — Lightning operations will fail")

    yield

    # Shutdown
    await lnd_client.disconnect()


app = FastAPI(
    title=settings.app_name,
    description="Lightning Payment Rails for AI Agents",
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

# Mount routers
app.include_router(wallets.router, prefix="/api/v1")
app.include_router(invoices.router, prefix="/api/v1")
app.include_router(payments.router, prefix="/api/v1")


@app.get("/health")
async def health_check() -> dict:
    """Basic health check endpoint."""
    return {
        "status": "healthy",
        "version": __version__,
        "service": settings.app_name,
    }


@app.get("/")
async def root() -> dict:
    """API root — service info."""
    return {
        "service": settings.app_name,
        "version": __version__,
        "docs": "/docs",
        "health": "/health",
    }
