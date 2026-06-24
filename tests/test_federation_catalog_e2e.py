"""End-to-end Federation #2 catalog tests against a REAL Postgres (+ in-process ASGI).

The cross-node loop the unit suite can't exercise: node A serves its active skills as
signed kind-38383 events over the real /api/v1/federation/skills endpoint; node B pulls
them through the real PeerCatalogTransport, re-verifies + caches (store_skill_events),
surfaces them in discovery (origin-tagged, badges neutralized, reputation overlay), and
refuses to execute them (is_cached_skill guard).

Opt-in: marked `e2e`, deselected by default. Run:

    ./.venv/bin/python -m pytest -m e2e -q

Needs a Postgres on localhost:5432 (the dedicated conduit_e2e database, created +
migrated by the e2e_db fixture). Skips cleanly if the DB is unreachable. B's HTTP pull
is routed at A's in-process ASGI app, so the SSRF host check is stubbed (no socket).
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from conduit.models.skill import Skill
from conduit.services.federation_catalog import (
    apply_reputation_overlay,
    get_cached_skills,
    is_cached_skill,
    merge_discovery,
    store_skill_events,
)
from conduit.services.nostr import NostrKeypair, skill_to_event

pytestmark = pytest.mark.e2e


@pytest.fixture
async def session(e2e_db) -> AsyncSession:
    """A real session against conduit_e2e, with the catalog tables truncated first."""
    engine = create_async_engine(e2e_db)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        await s.execute(text("TRUNCATE cached_skills, skills, federated_attestations CASCADE"))
        await s.commit()
        yield s
    await engine.dispose()


def _h(i: int) -> str:
    return f"{i:064x}"


async def test_two_node_serve_pull_discover_and_guard(session, monkeypatch):
    """Node A serves its catalog over the real endpoint; node B pulls, caches, discovers
    A's skill (origin=peer, badge neutralized), and is refused cross-node execution."""
    import httpx

    import conduit.services.catalog_transport as ct
    from conduit.api.deps import get_session
    from conduit.core.config import settings
    from conduit.main import app
    from conduit.services.node_identity import get_node_keypair

    monkeypatch.setattr(settings, "federation_enabled", True)

    # Node A's DB holds one active skill it will serve.
    a_skill_id = uuid.uuid4()
    session.add(
        Skill(
            id=a_skill_id, provider_name="Node A", name="Indexer",
            description="indexes things", category="data", price_sats=120,
            endpoint_url="https://a.example/api", is_active=True,
        )
    )
    await session.commit()
    node_a_pubkey = get_node_keypair().pubkey_hex  # the in-process node == node A

    # Back A's serve endpoint with the e2e DB; route B's HTTP client at A's ASGI app.
    async def _e2e_session():
        yield session

    app.dependency_overrides[get_session] = _e2e_session
    transport = httpx.ASGITransport(app=app)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda *a, **k: real_client(*a, **{**k, "transport": transport}),
    )
    monkeypatch.setattr(ct, "validate_outbound_url", lambda url: None)

    try:
        pulled = await ct.PeerCatalogTransport(["http://node-a"]).fetch_skills()
    finally:
        app.dependency_overrides.pop(get_session, None)

    # B reconstructs exactly the event A served, signed by node A.
    assert len(pulled) == 1
    assert pulled[0].pubkey == node_a_pubkey

    # B is a DIFFERENT node: store under B's key so A's listing isn't self-excluded.
    node_b = NostrKeypair.generate()
    n = await store_skill_events(session, pulled, self_pubkey=node_b.pubkey_hex, origin="peer")
    await session.commit()
    assert n == 1

    # B's discovery surfaces A's skill: origin-tagged, badge neutralized.
    items = merge_discovery([], await get_cached_skills(session), node_pubkey=node_b.pubkey_hex)
    assert len(items) == 1
    item = items[0]
    assert item["id"] == str(a_skill_id)
    assert item["origin"] == "peer"
    assert item["provider_pubkey"] == node_a_pubkey
    assert item["verification_status"] == "unverified"  # peer badge not trusted

    # B refuses to execute A's remote skill (cross-node execution is Federation #3).
    assert await is_cached_skill(session, str(a_skill_id)) is True


async def test_remote_skill_reputation_surfaces_in_discovery(session):
    """A cached remote skill carries its federated reputation through discovery."""
    from conduit.services.federation import build_rating_attestation, sign_payer_binding
    from conduit.services.federation_cache import store_events

    node_a, payer, node_b = (NostrKeypair.generate() for _ in range(3))
    skill_id = str(uuid.uuid4())

    # Cache a signed remote listing from node A (stored under B's key -> not self-excluded).
    listing = skill_to_event(
        {
            "id": skill_id, "name": "Remote Indexer", "category": "data",
            "price_sats": 200, "description": "d", "provider_name": "Node A",
            "provider_lightning_address": "", "endpoint_url": "", "tags": "",
        },
        node_a,
    )
    assert await store_skill_events(
        session, [listing], self_pubkey=node_b.pubkey_hex, origin="relay"
    ) == 1
    await session.commit()

    # Seed a verified attestation for (skill_id, node_a) into the reputation cache.
    binding = sign_payer_binding(
        skill_id=skill_id, payment_hash=_h(1),
        payer_pubkey=payer.pubkey_hex, provider_keypair=node_a,
    )
    att = build_rating_attestation(
        skill_id=skill_id, provider_pubkey=node_a.pubkey_hex, payment_hash=_h(1),
        score=5, payer_keypair=payer, provider_binding_sig=binding, created_at=1000,
    )
    await store_events(session, [att])
    await session.commit()

    # Discovery + overlay: the remote skill shows node A's federated reputation.
    items = merge_discovery([], await get_cached_skills(session), node_pubkey=node_b.pubkey_hex)
    await apply_reputation_overlay(session, items)
    assert len(items) == 1
    rep = items[0]["federated_reputation"]
    assert rep is not None
    assert rep["total_ratings"] == 1 and rep["score"] == 5.0
