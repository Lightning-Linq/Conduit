"""Tests for the Federation #2 catalog refresh wiring (Task 5).

refresh_catalog() pulls from both transports and routes each set to store_skill_events
with the right origin tag; the REST trigger reports both reputation + catalog counts.
Transports and the store are monkeypatched — the real fetch/verify/upsert are covered
by Tasks 2-3 and the Task 9 e2e.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

import conduit.services.federation_catalog as fc
from conduit.api.routers import federation as fed_router


class TestRefreshCatalog:
    async def test_pulls_both_transports_and_tags_origin(self, monkeypatch):
        calls = []

        class FakeNostr:
            def __init__(self, urls):
                pass

            async def fetch_skills(self, *, since, limit):
                return ["r1"]

        class FakePeer:
            def __init__(self, urls):
                pass

            async def fetch_skills(self, *, since, limit):
                return ["p1", "p2"]

        async def fake_store(session, events, *, self_pubkey=None, origin="relay", source_id=None):
            calls.append((origin, list(events)))
            return len(list(events))

        monkeypatch.setattr(fc, "NostrCatalogTransport", FakeNostr)
        monkeypatch.setattr(fc, "PeerCatalogTransport", FakePeer)
        monkeypatch.setattr(fc, "store_skill_events", fake_store)

        total = await fc.refresh_catalog(
            AsyncMock(), relay_urls=["wss://r"], peer_urls=["https://p"], self_pubkey="ab"
        )
        assert total == 3  # 1 relay + 2 peer, summed
        assert [origin for origin, _ in calls] == ["relay", "peer"]
        assert calls[0][1] == ["r1"] and calls[1][1] == ["p1", "p2"]


class TestTriggerRefreshEndpoint:
    async def test_reports_both_reputation_and_catalog_counts(self, monkeypatch):
        monkeypatch.setattr(fed_router.settings, "federation_enabled", True)

        async def fake_rep(session, **k):
            return 5

        async def fake_cat(session, **k):
            return 7

        monkeypatch.setattr(fed_router, "refresh_all_cached", fake_rep)
        monkeypatch.setattr(fed_router, "refresh_catalog", fake_cat)
        resp = await fed_router.trigger_refresh(session=AsyncMock())
        assert resp == {"refreshed": 5, "skills_cached": 7}

    async def test_404_when_federation_disabled(self, monkeypatch):
        monkeypatch.setattr(fed_router.settings, "federation_enabled", False)
        with pytest.raises(HTTPException) as exc:
            await fed_router.trigger_refresh(session=AsyncMock())
        assert exc.value.status_code == 404
