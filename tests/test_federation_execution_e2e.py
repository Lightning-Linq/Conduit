"""End-to-end Federation #3: node A buys a skill hosted by node B.

The loop the unit suite cannot exercise — a real broker against a real seller:

    A.request_skill_execution  -> HTTP -> B POST /api/v1/federation/executions
                                          (B mints real invoice rows)
    consumer pays B  (simulated: B's wallet reports the invoice settled)
    A.confirm_skill_execution  -> HTTP -> B .../{id}/confirm
                                          (B verifies settlement, runs the webhook)
    output flows B -> A -> consumer

TWO DATABASES on purpose. Sharing one would make B's skill LOCAL to A, so A would
execute it directly and the broker path — the thing under test — would never run.
conduit_e2e is node A, conduit_e2e_b is node B.

Opt-in: marked `e2e`, deselected by default. Run:

    ./.venv/bin/python -m pytest -m e2e -q

Needs Postgres on localhost:5432. Skips cleanly if it is unreachable. A's outbound
HTTP is routed at B's in-process ASGI app, so the SSRF host check is stubbed (no
socket is opened and no DNS lookup happens).
"""

import asyncio
import hashlib
import os
import pathlib
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from conduit.models.cached_skill import CachedSkill
from conduit.models.execution import ExecutionStatus, SkillExecution
from conduit.models.remote_execution import RemoteExecution
from conduit.models.skill import Skill

pytestmark = pytest.mark.e2e

B_ADMIN_URL = "postgresql+asyncpg://conduit:conduit@localhost:5432/conduit"
B_URL = "postgresql+asyncpg://conduit:conduit@localhost:5432/conduit_e2e_b"

# The consumer's payment secret. B's wallet stub issues an invoice whose hash is
# SHA256 of this, so both A's local precheck and B's C1 check see a real preimage.
PREIMAGE = "11" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()
FEE_HASH = "cd" * 32

PRICE_SATS = 1000  # -> 15 sats platform fee, 985 to the provider (1.5%, fee-inclusive)


async def _ensure_b_database() -> None:
    admin = create_async_engine(B_ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            exists = (
                await conn.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = 'conduit_e2e_b'")
                )
            ).scalar()
            if not exists:
                await conn.execute(text("CREATE DATABASE conduit_e2e_b"))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
def node_b_db() -> str:
    """Ensure + migrate node B's database; skip if Postgres is unreachable."""
    try:
        asyncio.run(_ensure_b_database())
    except Exception as exc:  # noqa: BLE001 - any connect failure => skip, not fail
        pytest.skip(f"Postgres not reachable for the node-B e2e: {exc}")

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=repo_root,
        env={**os.environ, "DATABASE_URL": B_URL},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(f"alembic upgrade failed for node B:\n{result.stderr[-800:]}")
    return B_URL


@pytest.fixture
async def a_session(e2e_db) -> AsyncSession:
    """Node A: holds the CACHED listing only (it does not host the skill)."""
    engine = create_async_engine(e2e_db)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("TRUNCATE cached_skills, remote_executions CASCADE"))
        await s.commit()
        yield s
    await engine.dispose()


@pytest.fixture
async def b_session(node_b_db) -> AsyncSession:
    """Node B: hosts the real skill and mints the invoices."""
    engine = create_async_engine(node_b_db)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("TRUNCATE skill_executions, skills CASCADE"))
        await s.commit()
        yield s
    await engine.dispose()


class _Invoice:
    def __init__(self, payment_request: str, payment_hash: str):
        self.payment_request = payment_request
        self.payment_hash = payment_hash


class _WalletStub:
    """B's wallet. The bolt11 strings encode REAL amounts, because A refuses a quote
    whose invoice does not decode to the amount the peer claims."""

    def __init__(self):
        self.created = []

    def create_invoice(self, *, amount_msats: int, memo: str = "", expiry: int = 600):
        sats = amount_msats // 1000
        self.created.append(sats)
        # n = 0.1 sat, so <sats*10>n encodes exactly `sats`. No '1' in the suffix:
        # bolt11 splits on the LAST '1', and a stray one would corrupt the amount.
        if "fee" in memo.lower():
            return _Invoice(f"lnbc{sats * 10}n1pfee", FEE_HASH)
        return _Invoice(f"lnbc{sats * 10}n1pfake", PAYMENT_HASH)

    def lookup_invoice(self, payment_hash: str) -> dict:
        return {"settled": True, "payment_hash": payment_hash}


async def test_node_a_buys_a_skill_hosted_by_node_b(a_session, b_session, monkeypatch):
    """The full cross-node purchase, through the real HTTP endpoints on both sides."""
    import httpx

    from conduit.api.deps import get_session
    from conduit.api.routers import marketplace as mkt
    from conduit.core.config import settings
    from conduit.main import app
    from conduit.services import federation_execution as fx

    monkeypatch.setattr(settings, "federation_enabled", True)
    monkeypatch.setattr(settings, "federation_execution_enabled", True)
    monkeypatch.setattr(settings, "federation_peers", "https://node-b")

    # ── Node B hosts the skill ────────────────────────────────────────
    skill_id = uuid.uuid4()
    b_session.add(
        Skill(
            id=skill_id, provider_name="Node B", name="Remote Indexer",
            description="indexes things", category="data", price_sats=PRICE_SATS,
            endpoint_url="https://b.example/api", is_active=True,
        )
    )
    await b_session.commit()

    # ── Node A only knows it from the federated catalog ───────────────
    a_session.add(
        CachedSkill(
            provider_pubkey="ab" * 32, skill_id=str(skill_id), event_id="cd" * 32,
            event_created_at=1, origin="peer", source_id="https://node-b",
            name="Remote Indexer", description="indexes things", category="data",
            price_sats=PRICE_SATS, raw_event={},
        )
    )
    await a_session.commit()

    # ── Wire B's app behind A's HTTP client ───────────────────────────
    wallet = _WalletStub()
    monkeypatch.setattr(mkt, "get_lnd", lambda: wallet)
    monkeypatch.setattr(mkt, "get_platform_wallet", lambda: None)  # fee via local wallet

    async def _b_session_dep():
        yield b_session

    app.dependency_overrides[get_session] = _b_session_dep
    transport = httpx.ASGITransport(app=app)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        fx, "validate_outbound_url", lambda url: None  # node-b is not a real host
    )
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": transport}),
    )

    # The provider webhook: B calls it during confirm.
    async def fake_webhook(**kwargs):
        return {"output": {"rows": 42, "for": kwargs.get("skill_name")},
                "execution_time_ms": 12}

    monkeypatch.setattr(mkt, "execute_skill_webhook", fake_webhook)

    try:
        # ── 1. A brokers the purchase ─────────────────────────────────
        quote = await mkt.request_skill_execution(
            mkt.RequestExecutionRequest(
                skill_id=str(skill_id), consumer_name="agent-on-a",
                input_data={"q": "hello"},
            ),
            a_session,
        )

        # A relays B's invoice, tells the caller who hosts it, and takes no custody.
        assert quote["origin"] == "peer"
        assert quote["host_node"] == "https://node-b"
        assert quote["payment_request"] == "lnbc9850n1pfake"  # 985 sats, fee-inclusive
        assert quote["price_sats"] == PRICE_SATS
        assert quote["platform_fee_sats"] == 15
        assert quote["provider_receives_sats"] == 985
        # B minted both invoices on ITS wallet — A minted nothing.
        assert wallet.created == [15, 985]

        # A's local record points at B; B has its own PENDING_PAYMENT execution.
        local_id = uuid.UUID(quote["execution_id"])
        row = await a_session.get(RemoteExecution, local_id)
        assert row is not None and row.peer_url == "https://node-b"
        assert row.status == ExecutionStatus.PENDING_PAYMENT

        b_exec = await b_session.get(SkillExecution, uuid.UUID(row.remote_execution_id))
        assert b_exec is not None and b_exec.status == ExecutionStatus.PENDING_PAYMENT
        assert str(local_id) != row.remote_execution_id  # ids are per-node

        # ── 2. The consumer pays B, then confirms with A ──────────────
        result = await mkt.confirm_skill_execution(
            str(local_id),
            mkt.ConfirmExecutionRequest(
                payment_hash=PAYMENT_HASH, payment_preimage=PREIMAGE
            ),
            a_session,
        )
    finally:
        app.dependency_overrides.pop(get_session, None)

    # ── 3. B executed; the output came back through A ─────────────────
    assert result["status"] == ExecutionStatus.COMPLETED.value
    assert result["output"] == {"rows": 42, "for": "Remote Indexer"}
    assert result["origin"] == "peer" and result["host_node"] == "https://node-b"

    await a_session.refresh(row)
    assert row.status == ExecutionStatus.COMPLETED
    assert row.output_data == {"rows": 42, "for": "Remote Indexer"}

    await b_session.refresh(b_exec)
    assert b_exec.status == ExecutionStatus.COMPLETED
    assert b_exec.payment_preimage == PREIMAGE  # B verified the payment itself


async def test_node_b_refuses_to_broker_onward(a_session, b_session, monkeypatch):
    """B must not chain a purchase to a third node.

    B is given the same CACHED listing A has (and no local skill), then asked to sell
    it over the real federation endpoint. It must refuse rather than become a broker,
    which is what stops A -> B -> C fan-out.
    """
    import httpx

    from conduit.api.deps import get_session
    from conduit.core.config import settings
    from conduit.main import app

    monkeypatch.setattr(settings, "federation_enabled", True)
    monkeypatch.setattr(settings, "federation_execution_enabled", True)

    skill_id = uuid.uuid4()
    b_session.add(
        CachedSkill(
            provider_pubkey="ab" * 32, skill_id=str(skill_id), event_id="ef" * 32,
            event_created_at=1, origin="peer", source_id="https://node-c",
            name="Someone Else's Skill", category="data", price_sats=PRICE_SATS,
            raw_event={},
        )
    )
    await b_session.commit()

    async def _b_session_dep():
        yield b_session

    app.dependency_overrides[get_session] = _b_session_dep
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://node-b"
        ) as client:
            resp = await client.post(
                "/api/v1/federation/executions",
                json={"skill_id": str(skill_id), "consumer_name": "agent-on-a"},
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        await b_session.execute(text("TRUNCATE cached_skills CASCADE"))
        await b_session.commit()

    assert resp.status_code == 501
    assert "does not broker" in resp.json()["detail"]


async def test_a_refuses_an_inflated_invoice_from_b(a_session, b_session, monkeypatch):
    """The bait-and-switch guard, end to end against a lying seller.

    B quotes the listed price in JSON but mints an invoice for 500,000 sats. A must
    refuse before the consumer sees a payment request, and must leave no local row —
    an agent would otherwise pay the invoice without a human reading the amount.
    """
    import httpx
    from fastapi import HTTPException
    from sqlalchemy import select

    from conduit.api.deps import get_session
    from conduit.api.routers import marketplace as mkt
    from conduit.core.config import settings
    from conduit.main import app
    from conduit.services import federation_execution as fx

    monkeypatch.setattr(settings, "federation_enabled", True)
    monkeypatch.setattr(settings, "federation_execution_enabled", True)
    monkeypatch.setattr(settings, "federation_peers", "https://node-b")

    skill_id = uuid.uuid4()
    b_session.add(
        Skill(
            id=skill_id, provider_name="Node B", name="Remote Indexer",
            description="indexes things", category="data", price_sats=PRICE_SATS,
            endpoint_url="https://b.example/api", is_active=True,
        )
    )
    await b_session.commit()

    a_session.add(
        CachedSkill(
            provider_pubkey="ab" * 32, skill_id=str(skill_id), event_id="cd" * 32,
            event_created_at=1, origin="peer", source_id="https://node-b",
            name="Remote Indexer", category="data", price_sats=PRICE_SATS, raw_event={},
        )
    )
    await a_session.commit()

    class _LyingWallet(_WalletStub):
        def create_invoice(self, *, amount_msats: int, memo: str = "", expiry: int = 600):
            inv = super().create_invoice(amount_msats=amount_msats, memo=memo, expiry=expiry)
            if "fee" not in memo.lower():
                inv.payment_request = "lnbc5m1pfake"  # 500,000 sats, not 985
            return inv

    monkeypatch.setattr(mkt, "get_lnd", lambda: _LyingWallet())
    monkeypatch.setattr(mkt, "get_platform_wallet", lambda: None)

    async def _b_session_dep():
        yield b_session

    app.dependency_overrides[get_session] = _b_session_dep
    real_client = httpx.AsyncClient
    monkeypatch.setattr(fx, "validate_outbound_url", lambda url: None)
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": httpx.ASGITransport(app=app)}),
    )

    try:
        with pytest.raises(HTTPException) as exc:
            await mkt.request_skill_execution(
                mkt.RequestExecutionRequest(
                    skill_id=str(skill_id), consumer_name="agent-on-a"
                ),
                a_session,
            )
    finally:
        app.dependency_overrides.pop(get_session, None)

    assert exc.value.status_code == 502
    assert "invoice" in str(exc.value.detail).lower()

    # Nothing persisted on A: no half-open purchase to stumble into confirming.
    rows = (await a_session.execute(select(RemoteExecution))).scalars().all()
    assert rows == []
