"""Tests for the Federation #2 catalog serve endpoint (Task 4).

The serve path turns this node's active skills into freshly signed kind-38383 events
that a peer's PeerCatalogTransport pulls and re-verifies. Tested at the conversion +
builder + handler level (the full two-node HTTP path is the Task 9 e2e); the DB query
is exercised with a mocked session, matching tests/test_api.py.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from conduit.api.routers import federation as fed_router
from conduit.models.skill import Skill
from conduit.services.federation_catalog import (
    _local_skill_to_event,
    get_local_skill_events,
)
from conduit.services.nostr import NostrEvent, NostrKeypair, event_to_skill


def _skill(**over) -> Skill:
    s = Skill(
        provider_name="Prov",
        provider_lightning_address="prov@ln.tld",
        name="Indexer",
        description="indexes things",
        category="data",
        tags="ai,data",
        price_sats=250,
        endpoint_url="https://example.com/api",
        input_schema={"type": "object"},
        output_schema=None,
        is_active=True,
    )
    s.id = over.pop("id", uuid.uuid4())
    for k, v in over.items():
        setattr(s, k, v)
    return s


def _session_returning(skills) -> AsyncMock:
    """An AsyncMock session whose query returns the given rows (per tests/test_api.py)."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = skills
    session.execute = AsyncMock(return_value=result)
    return session


class TestLocalSkillToEvent:
    def test_signs_verifiable_kind38383_that_round_trips(self):
        kp = NostrKeypair.generate()
        s = _skill(name="Indexer", price_sats=250)
        ev = _local_skill_to_event(s, kp)
        assert ev.verify()  # node-signed, integrity intact
        assert ev.pubkey == kp.pubkey_hex
        parsed = event_to_skill(ev)  # a puller parses it straight back
        assert parsed["id"] == str(s.id)
        assert parsed["name"] == "Indexer"
        assert parsed["price_sats"] == 250
        assert parsed["provider_lightning_address"] == "prov@ln.tld"


class TestGetLocalSkillEvents:
    async def test_returns_signed_event_dicts(self):
        kp = NostrKeypair.generate()
        session = _session_returning([_skill(name="A"), _skill(name="B")])
        out = await get_local_skill_events(session, since=0, limit=500, keypair=kp)
        assert len(out) == 2
        evs = [NostrEvent.from_dict(d) for d in out]  # JSON-ready dicts
        assert all(e.verify() for e in evs)
        assert {event_to_skill(e)["name"] for e in evs} == {"A", "B"}


class TestServeSkillsHandler:
    async def test_404_when_federation_disabled(self, monkeypatch):
        monkeypatch.setattr(fed_router.settings, "federation_enabled", False)
        with pytest.raises(HTTPException) as exc:
            await fed_router.serve_skills(since=0, limit=500, session=AsyncMock())
        assert exc.value.status_code == 404

    async def test_serves_payload_when_enabled(self, monkeypatch):
        monkeypatch.setattr(fed_router.settings, "federation_enabled", True)
        session = _session_returning([_skill(name="A")])
        resp = await fed_router.serve_skills(since=0, limit=500, session=session)
        assert resp["count"] == 1
        assert len(resp["skills"]) == 1
        assert NostrEvent.from_dict(resp["skills"][0]).verify()
