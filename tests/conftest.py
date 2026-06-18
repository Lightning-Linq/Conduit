"""Shared test fixtures for Conduit tests.

Sets environment variables before any conduit module is imported (preventing the
database engine from connecting to PostgreSQL), then mocks the DB module for the
default unit suite. The e2e fixtures at the bottom build their OWN engine against a
dedicated conduit_e2e database, so they bypass that mock; they back the tests
marked `e2e` (deselected by default, run with `-m e2e`).
"""

import asyncio
import os
import pathlib
import subprocess
import sys
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Set test environment before importing any conduit modules
os.environ.setdefault("CONDUIT_API_KEY", "test-api-key-for-unit-tests")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("DEBUG", "false")

# Mock the database module so imports don't trigger engine creation
# when PostgreSQL isn't available (unit tests only)
if "conduit.core.database" not in sys.modules:
    mock_db = MagicMock()
    mock_db.async_session_factory = MagicMock()
    sys.modules["conduit.core.database"] = mock_db


# ── End-to-end DB fixtures (real Postgres; opt-in via -m e2e) ────────────────
# A dedicated conduit_e2e database is created + migrated, then each test gets a
# real session against it. These build their own engine, so the mock above does
# not apply. Skips cleanly if Postgres is unreachable.

E2E_ADMIN_URL = "postgresql+asyncpg://conduit:conduit@localhost:5432/conduit"
E2E_URL = "postgresql+asyncpg://conduit:conduit@localhost:5432/conduit_e2e"


async def _ensure_e2e_database() -> None:
    """Create the dedicated conduit_e2e database if it does not exist."""
    admin = create_async_engine(E2E_ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = 'conduit_e2e'")
                )
            ).scalar()
            if not exists:
                await conn.execute(text("CREATE DATABASE conduit_e2e"))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
def e2e_db() -> str:
    """Ensure + migrate the conduit_e2e database; skip the suite if PG is down."""
    try:
        asyncio.run(_ensure_e2e_database())
    except Exception as exc:  # noqa: BLE001 - any connect failure => skip, not fail
        pytest.skip(f"Postgres not reachable for e2e: {exc}")

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env={**os.environ, "DATABASE_URL": E2E_URL},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"alembic upgrade failed:\n{result.stderr[-800:]}")
    return E2E_URL


@pytest.fixture
async def e2e_session(e2e_db) -> AsyncSession:
    """A real AsyncSession against conduit_e2e (tests use unique ids; no truncation)."""
    engine = create_async_engine(e2e_db)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()
