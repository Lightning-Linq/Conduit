"""End-to-end federation tests against a REAL Postgres + a local Nostr relay.

These exercise the integration glue the unit suite can't (conftest mocks the DB
and monkeypatches the relay): the actual SQL + migrations, the websocket relay
protocol, and the full cross-node loop mint -> build -> publish -> fetch -> verify
-> cache -> aggregate.

Opt-in: marked `e2e` and deselected by default (`addopts = -m 'not e2e'`). Run with:

    ./.venv/bin/python -m pytest -m e2e -q

Needs a Postgres on localhost:5432 (the docker-compose `conduit`/`conduit` one);
a dedicated `conduit_e2e` database is created and migrated. Skips cleanly if the
DB is unreachable. The relay is a tiny in-process websocket server, so the SSRF
guard is bypassed via validate_relays=False (ws:// loopback).
"""

import asyncio
import json
import os
import pathlib
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from conduit.services.federation import (
    build_rating_attestation,
    fetch_ratings,
    mint_execution_binding,
    publish_rating,
    sign_payer_binding,
)
from conduit.services.federation_cache import (
    get_cached_reputation,
    store_events,
    submit_attestation,
)
from conduit.services.nostr import NostrKeypair

pytestmark = pytest.mark.e2e

ADMIN_URL = "postgresql+asyncpg://conduit:conduit@localhost:5432/conduit"
E2E_URL = "postgresql+asyncpg://conduit:conduit@localhost:5432/conduit_e2e"


# ── Local Nostr relay (minimal NIP-01: EVENT store + REQ match + EOSE) ──────


def _event_matches(event: dict, filt: dict) -> bool:
    if "kinds" in filt and event.get("kind") not in filt["kinds"]:
        return False
    for key, vals in filt.items():
        if key.startswith("#") and len(key) == 2:  # single-letter tag filter, e.g. #p
            present = [t[1] for t in event.get("tags", []) if len(t) >= 2 and t[0] == key[1]]
            if not any(v in present for v in vals):
                return False
    return True


class LocalRelay:
    """In-process relay: stores published events, answers REQ with matches + EOSE."""

    def __init__(self):
        self.events: list[dict] = []
        self._server = None
        self.url = ""

    async def _handle(self, ws):
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, list) or not msg:
                continue
            if msg[0] == "EVENT" and len(msg) >= 2:
                self.events.append(msg[1])
                await ws.send(json.dumps(["OK", msg[1].get("id", ""), True, ""]))
            elif msg[0] == "REQ" and len(msg) >= 3:
                sub_id, filters = msg[1], msg[2:]
                for event in self.events:
                    if any(_event_matches(event, f) for f in filters):
                        await ws.send(json.dumps(["EVENT", sub_id, event]))
                await ws.send(json.dumps(["EOSE", sub_id]))

    async def start(self):
        import websockets

        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"

    async def stop(self):
        self._server.close()
        await self._server.wait_closed()


# ── Fixtures ────────────────────────────────────────────────────────────────


async def _ensure_database() -> None:
    """Create the dedicated conduit_e2e database if it doesn't exist."""
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
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
        asyncio.run(_ensure_database())
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
async def session(e2e_db) -> AsyncSession:
    """A real session against conduit_e2e, with the cache table truncated first."""
    engine = create_async_engine(e2e_db)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("TRUNCATE federated_attestations"))
        await s.commit()
        yield s
    await engine.dispose()


@pytest.fixture
async def relay() -> LocalRelay:
    r = LocalRelay()
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


# ── Helpers ─────────────────────────────────────────────────────────────────


def _h(i: int) -> str:
    return f"{i:064x}"


def _attestation(provider, payer, score, payment_hash, skill_id):
    binding = sign_payer_binding(
        skill_id=skill_id, payment_hash=payment_hash,
        payer_pubkey=payer.pubkey_hex, provider_keypair=provider,
    )
    return build_rating_attestation(
        skill_id=skill_id, provider_pubkey=provider.pubkey_hex, payment_hash=payment_hash,
        score=score, payer_keypair=payer, provider_binding_sig=binding, created_at=1000,
    )


# ── Tests ───────────────────────────────────────────────────────────────────


async def test_cache_roundtrip_and_idempotency(session):
    """Real SQL: store -> aggregate, and a re-store is a no-op (ON CONFLICT)."""
    skill = str(uuid.uuid4())
    prov, a, b = (NostrKeypair.generate() for _ in range(3))
    events = [_attestation(prov, a, 5, _h(1), skill), _attestation(prov, b, 3, _h(2), skill)]
    await store_events(session, events)
    await session.commit()
    await store_events(session, events)  # idempotent re-store
    await session.commit()

    count = (
        await session.execute(
            text("SELECT count(*) FROM federated_attestations WHERE provider_pubkey = :p"),
            {"p": prov.pubkey_hex},
        )
    ).scalar()
    assert count == 2  # ON CONFLICT DO NOTHING held

    agg = await get_cached_reputation(
        session, skill_id=skill, provider_pubkey=prov.pubkey_hex, use_web_of_trust=True
    )
    assert agg.score == 4.0 and agg.distinct_payers == 2 and agg.total_ratings == 2


async def test_transport_publish_then_fetch(session, relay):
    """Real websocket wire: publish a rating, fetch it back by provider."""
    skill = str(uuid.uuid4())
    prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
    event = _attestation(prov, payer, 5, _h(1), skill)

    result = await publish_rating(event, [relay.url], validate_relays=False)
    assert result.get(relay.url) is True

    fetched = await fetch_ratings(prov.pubkey_hex, [relay.url], validate_relays=False)
    assert event.id in {e.id for e in fetched}


async def test_fetch_filters_by_provider(relay):
    """The #p relay filter returns only the requested provider's attestations."""
    skill = str(uuid.uuid4())
    p1, p2, payer = (NostrKeypair.generate() for _ in range(3))
    e1 = _attestation(p1, payer, 5, _h(1), skill)
    e2 = _attestation(p2, payer, 4, _h(2), skill)
    await publish_rating(e1, [relay.url], validate_relays=False)
    await publish_rating(e2, [relay.url], validate_relays=False)

    got = {e.id for e in await fetch_ratings(p1.pubkey_hex, [relay.url], validate_relays=False)}
    assert e1.id in got and e2.id not in got


async def test_cross_node_full_loop(session, relay):
    """mint (provider) -> build (consumer) -> publish -> fetch -> store -> aggregate."""
    skill = str(uuid.uuid4())
    provider = NostrKeypair.generate()   # the provider node (skill owner + binding signer)
    consumer = NostrKeypair.generate()   # the consumer/payer node (different key)
    payment_hash = _h(7)

    # Provider mints the binding; consumer signs the rating with its own key.
    binding = mint_execution_binding(
        skill_id=skill, payment_hash=payment_hash,
        payer_pubkey=consumer.pubkey_hex, provider_keypair=provider,
    )
    event = build_rating_attestation(
        skill_id=skill, provider_pubkey=provider.pubkey_hex, payment_hash=payment_hash,
        score=5, payer_keypair=consumer, provider_binding_sig=binding, created_at=1000,
    )

    # Consumer publishes; a third node fetches, verifies, caches, aggregates.
    await publish_rating(event, [relay.url], validate_relays=False)
    fetched = await fetch_ratings(provider.pubkey_hex, [relay.url], validate_relays=False)
    assert event.id in {e.id for e in fetched}

    await store_events(session, fetched)  # re-verifies on the way in
    await session.commit()

    agg = await get_cached_reputation(
        session, skill_id=skill, provider_pubkey=provider.pubkey_hex, use_web_of_trust=False
    )
    assert agg.score == 5.0 and agg.total_ratings == 1 and agg.distinct_payers == 1


async def test_submit_attestation_caches_and_returns(session):
    """submit_attestation verifies + caches and hands the event back to publish."""
    skill = str(uuid.uuid4())
    prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
    event = _attestation(prov, payer, 5, _h(1), skill)
    returned = await submit_attestation(
        session, event, skill_id=skill, provider_pubkey=prov.pubkey_hex,
        payment_hash=_h(1), payer_pubkey=payer.pubkey_hex, expected_score=5,
    )
    await session.commit()
    assert returned is not None and returned.id == event.id
    agg = await get_cached_reputation(
        session, skill_id=skill, provider_pubkey=prov.pubkey_hex, use_web_of_trust=False
    )
    assert agg.total_ratings == 1 and agg.score == 5.0


async def test_submit_attestation_rejects_score_mismatch(session):
    """A pre-signed event whose score disagrees with the local rating is not cached."""
    skill = str(uuid.uuid4())
    prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
    event = _attestation(prov, payer, 1, _h(1), skill)  # signed score 1
    returned = await submit_attestation(
        session, event, skill_id=skill, provider_pubkey=prov.pubkey_hex,
        payment_hash=_h(1), payer_pubkey=payer.pubkey_hex, expected_score=5,  # local says 5
    )
    await session.commit()
    assert returned is None
    agg = await get_cached_reputation(
        session, skill_id=skill, provider_pubkey=prov.pubkey_hex, use_web_of_trust=False
    )
    assert agg.total_ratings == 0  # nothing cached


async def test_submit_attestation_rejects_wrong_execution(session):
    """The match guard: an attestation for a different payment is not cached."""
    skill = str(uuid.uuid4())
    prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
    event = _attestation(prov, payer, 5, _h(1), skill)  # really for payment _h(1)
    returned = await submit_attestation(
        session, event, skill_id=skill, provider_pubkey=prov.pubkey_hex,
        payment_hash=_h(2), payer_pubkey=payer.pubkey_hex,  # claims a different payment
    )
    await session.commit()
    assert returned is None
