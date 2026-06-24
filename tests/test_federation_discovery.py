"""Tests for Federation #2 discovery merge (Task 6, the headline behavior).

merge_discovery() folds cached remote skills into local discovery results: dedup by
(provider_pubkey, skill_id) preferring local, origin-tag each result, and NEUTRALIZE
remote verification badges (a peer is not trusted to assert verification). Pure — no DB.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from conduit.api.routers import marketplace as mkt
from conduit.services.federation_catalog import apply_reputation_overlay, merge_discovery

NODE = "ab" * 32  # this node's Nostr pubkey (64 hex)


def _local(**kw) -> SimpleNamespace:
    base = dict(
        id=uuid.uuid4(), name="Local", description="d", provider_name="Me",
        category="data", price_sats=100, verification_status="node_verified",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _cached(**kw) -> SimpleNamespace:
    base = dict(
        skill_id="remote-skill", provider_pubkey="cd" * 32, origin="peer",
        name="Remote", description="d", provider_name="Them",
        category="data", price_sats=200,
    )
    base.update(kw)
    return SimpleNamespace(**base)


class TestMergeDiscovery:
    def test_tags_origin_and_keeps_local_badge(self):
        out = merge_discovery(
            [_local(verification_status="fully_verified")],
            [_cached(origin="relay")],
            node_pubkey=NODE,
        )
        local, remote = out[0], out[1]
        assert local["origin"] == "local"
        assert local["verification_status"] == "fully_verified"  # local badge is real
        assert local["provider_pubkey"] == NODE
        assert remote["origin"] == "relay"
        assert remote["provider_pubkey"] == "cd" * 32

    def test_neutralizes_remote_verification_badge(self):
        out = merge_discovery([], [_cached()], node_pubkey=NODE)
        assert out[0]["verification_status"] == "unverified"  # peer claim NOT trusted

    def test_dedup_prefers_local(self):
        sid = uuid.uuid4()
        clash = _cached(provider_pubkey=NODE, skill_id=str(sid))  # same coordinate as local
        out = merge_discovery([_local(id=sid)], [clash], node_pubkey=NODE)
        assert len(out) == 1 and out[0]["origin"] == "local"

    def test_max_price_filters_remote(self):
        out = merge_discovery([], [_cached(price_sats=500)], node_pubkey=NODE, max_price=200)
        assert out == []


def _session_returning(skills) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = skills
    session.execute = AsyncMock(return_value=result)
    return session


class TestDiscoverSkillsEndpoint:
    async def test_merges_local_and_cached(self, monkeypatch):
        monkeypatch.setattr(mkt.settings, "federation_enabled", True)

        async def fake_cached(session, **k):
            return [_cached(name="RemoteSkill", origin="peer")]

        monkeypatch.setattr(mkt, "get_cached_skills", fake_cached)
        resp = await mkt.discover_skills(
            keyword="", category="", max_price=0,
            session=_session_returning([_local(name="LocalSkill")]),
        )
        by_name = {s["name"]: s for s in resp["skills"]}
        assert resp["count"] == 2
        assert by_name["LocalSkill"]["origin"] == "local"
        assert by_name["RemoteSkill"]["origin"] == "peer"
        assert by_name["RemoteSkill"]["verification_status"] == "unverified"
        assert all("federated_reputation" in s for s in resp["skills"])

    async def test_federation_disabled_is_local_only(self, monkeypatch):
        monkeypatch.setattr(mkt.settings, "federation_enabled", False)

        async def boom(*a, **k):
            raise AssertionError("federation disabled — cache must not be queried")

        monkeypatch.setattr(mkt, "get_cached_skills", boom)
        resp = await mkt.discover_skills(
            keyword="", category="", max_price=0,
            session=_session_returning([_local(name="LocalOnly")]),
        )
        assert resp["count"] == 1 and resp["skills"][0]["origin"] == "local"

    async def test_federation_error_is_fail_soft(self, monkeypatch):
        monkeypatch.setattr(mkt.settings, "federation_enabled", True)

        async def boom(session, **k):
            raise RuntimeError("cache down")

        monkeypatch.setattr(mkt, "get_cached_skills", boom)
        resp = await mkt.discover_skills(
            keyword="", category="", max_price=0,
            session=_session_returning([_local(name="LocalSkill")]),
        )
        assert resp["count"] == 1 and resp["skills"][0]["name"] == "LocalSkill"


class TestReputationOverlay:
    async def test_attaches_score_or_none_per_item(self, monkeypatch):
        import conduit.services.federation_cache as fcache

        async def fake_rep(session, *, skill_id, provider_pubkey, use_web_of_trust=False):
            if skill_id == "has-rep":
                return SimpleNamespace(score=4.5, distinct_payers=3, total_ratings=5, flags=[])
            return SimpleNamespace(score=0.0, distinct_payers=0, total_ratings=0, flags=[])

        monkeypatch.setattr(fcache, "get_cached_reputation", fake_rep)
        items = [
            {"id": "has-rep", "provider_pubkey": "ab"},
            {"id": "no-rep", "provider_pubkey": "cd"},
        ]
        await apply_reputation_overlay(AsyncMock(), items)
        assert items[0]["federated_reputation"]["score"] == 4.5
        assert items[0]["federated_reputation"]["total_ratings"] == 5
        assert items[1]["federated_reputation"] is None  # no ratings -> None

    async def test_fail_soft_on_read_error(self, monkeypatch):
        import conduit.services.federation_cache as fcache

        async def boom(session, **k):
            raise RuntimeError("cache down")

        monkeypatch.setattr(fcache, "get_cached_reputation", boom)
        items = [{"id": "x", "provider_pubkey": "ab"}]
        await apply_reputation_overlay(AsyncMock(), items)
        assert items[0]["federated_reputation"] is None
