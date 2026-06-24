"""Tests for the Federation #2 catalog transports (Task 3).

Two sources, one shape: both return RAW kind-38383 events for the cache to re-verify.
Relay fetch is monkeypatched at _subscribe_across_relays; peer fetch fakes httpx.
SSRF: unsafe relay/peer URLs are dropped BEFORE any network call (asserted, not
inferred from a swallowed connection error).
"""

import httpx

from conduit.services import catalog_transport as ct
from conduit.services.nostr import SKILL_EVENT_KIND, NostrKeypair, skill_to_event

SK1 = "11111111-1111-1111-1111-111111111111"
SK2 = "22222222-2222-2222-2222-222222222222"


def _skill_event(keypair, *, skill_id, name="Test Skill", created_at=1000):
    skill = {
        "id": skill_id, "name": name, "category": "data", "price_sats": 100,
        "description": "a skill", "provider_name": "Prov",
        "endpoint_url": "https://example.com/api", "tags": "ai,data",
    }
    ev = skill_to_event(skill, keypair)
    ev.created_at = created_at
    ev.sign(keypair)
    return ev


class TestNostrCatalogTransport:
    async def test_returns_raw_events_with_skill_filter(self, monkeypatch):
        prov = NostrKeypair.generate()
        ev = _skill_event(prov, skill_id=SK1)
        seen = {}

        async def fake_sub(filters, urls, *, timeout, max_events, pin_dns):
            seen["filter"] = filters[0]
            seen["pin_dns"] = pin_dns
            return [ev]

        monkeypatch.setattr(ct, "_subscribe_across_relays", fake_sub)
        t = ct.NostrCatalogTransport(["wss://relay.damus.io"])  # in the allowlist
        out = await t.fetch_skills(since=1000, limit=10)

        assert [e.id for e in out] == [ev.id]
        assert seen["filter"]["kinds"] == [SKILL_EVENT_KIND]
        assert seen["filter"]["since"] == 1000
        assert seen["pin_dns"] is True

    async def test_drops_unsafe_relays_without_dialing(self, monkeypatch):
        called = False

        async def fake_sub(*a, **k):
            nonlocal called
            called = True
            return []

        monkeypatch.setattr(ct, "_subscribe_across_relays", fake_sub)
        t = ct.NostrCatalogTransport(["ws://1.2.3.4", "wss://10.0.0.1"])  # bad scheme / internal
        out = await t.fetch_skills()
        assert out == [] and called is False


class TestPeerCatalogTransport:
    async def test_parses_skills_from_peer(self, monkeypatch):
        prov = NostrKeypair.generate()
        e1, e2 = _skill_event(prov, skill_id=SK1), _skill_event(prov, skill_id=SK2)
        payload = {"skills": [e1.to_dict(), e2.to_dict()]}

        class _Resp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, params=None):
                return _Resp()

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        t = ct.PeerCatalogTransport(["https://1.1.1.1"])
        events = await t.fetch_skills()
        assert {e.id for e in events} == {e1.id, e2.id}

    async def test_skips_unsafe_peers_without_dialing(self, monkeypatch):
        called = False

        class _Client:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, *a, **k):
                nonlocal called
                called = True
                raise AssertionError("must not dial an unsafe peer")

        monkeypatch.setattr(httpx, "AsyncClient", _Client)
        t = ct.PeerCatalogTransport(["http://1.1.1.1", "https://10.0.0.1"])  # not https / internal
        out = await t.fetch_skills()
        assert out == [] and called is False


def test_both_transports_satisfy_protocol():
    assert isinstance(ct.NostrCatalogTransport(), ct.CatalogTransport)
    assert isinstance(ct.PeerCatalogTransport(), ct.CatalogTransport)
