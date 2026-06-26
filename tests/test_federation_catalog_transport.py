"""Tests for the Federation #2 catalog transports (Task 3).

Two sources, one shape: both return RAW kind-38383 events for the cache to re-verify.
Relay fetch is monkeypatched at _subscribe_across_relays; peer fetch fakes httpx.
SSRF: unsafe relay/peer URLs are dropped BEFORE any network call (asserted, not
inferred from a swallowed connection error).
"""

import json

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


class _FakeStreamResp:
    """A fake httpx streaming response (async CM) for PeerCatalogTransport tests.

    Mirrors the slice of httpx the transport uses: ``raise_for_status()``,
    ``headers``, and ``aiter_bytes()`` (chunked, to exercise the running-total
    cap). ``on_read`` fires when the body is first streamed, so a test can prove
    the body was NOT read (e.g. when Content-Length already tripped the cap).
    """

    def __init__(self, body: bytes = b"", *, headers=None, on_read=None):
        self._body = body
        self.headers = headers or {}
        self._on_read = on_read

    def raise_for_status(self):
        pass

    async def aiter_bytes(self):
        if self._on_read is not None:
            self._on_read()
        for i in range(0, len(self._body), 16):
            yield self._body[i : i + 16]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake_httpx(route):
    """A fake httpx.AsyncClient whose .stream(method, url, params) -> route(url)."""

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, params=None, headers=None):
            return route(url)

    return _Client


class TestPeerCatalogTransport:
    async def test_parses_skills_from_peer(self, monkeypatch):
        prov = NostrKeypair.generate()
        e1, e2 = _skill_event(prov, skill_id=SK1), _skill_event(prov, skill_id=SK2)
        body = json.dumps({"skills": [e1.to_dict(), e2.to_dict()]}).encode()
        monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx(lambda url: _FakeStreamResp(body)))
        t = ct.PeerCatalogTransport(["https://1.1.1.1"])
        events = await t.fetch_skills()
        assert {e.id for e in events} == {e1.id, e2.id}

    async def test_skips_unsafe_peers_without_dialing(self, monkeypatch):
        called = False

        def route(url):
            nonlocal called
            called = True
            return _FakeStreamResp(b'{"skills":[]}')

        monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx(route))
        t = ct.PeerCatalogTransport(["http://1.1.1.1", "https://10.0.0.1"])  # not https / internal
        out = await t.fetch_skills()
        assert out == [] and called is False

    async def test_skips_peer_response_over_size_cap(self, monkeypatch):
        """An oversize streamed body (no/understated Content-Length) is dropped mid-read;
        a good peer in the same batch still returns its events."""
        prov = NostrKeypair.generate()
        e1, e2 = _skill_event(prov, skill_id=SK1), _skill_event(prov, skill_id=SK2)
        good_body = json.dumps({"skills": [e1.to_dict(), e2.to_dict()]}).encode()
        cap = len(good_body) + 1024
        over_body = b"x" * (cap + 1)  # no Content-Length -> caught by the streaming byte-count

        def route(url):
            # 1.1.1.1 = the good peer; 8.8.8.8 = the oversize peer (both public -> pass SSRF).
            return _FakeStreamResp(good_body if "1.1.1.1" in url else over_body)

        monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx(route))
        t = ct.PeerCatalogTransport(
            ["https://1.1.1.1", "https://8.8.8.8"], max_response_bytes=cap
        )
        events = await t.fetch_skills()
        assert {e.id for e in events} == {e1.id, e2.id}

    async def test_skips_peer_when_content_length_exceeds_cap(self, monkeypatch):
        """An over-cap Content-Length is rejected up front — the body is never streamed."""
        read = False

        def _mark_read():
            nonlocal read
            read = True

        resp = _FakeStreamResp(
            b"x" * 10_000, headers={"content-length": "10000"}, on_read=_mark_read
        )
        monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx(lambda url: resp))
        t = ct.PeerCatalogTransport(["https://1.1.1.1"], max_response_bytes=100)
        out = await t.fetch_skills()
        assert out == [] and read is False

    async def test_skips_compressed_peer_without_decoding(self, monkeypatch):
        """A Content-Encoding body is refused before reading (gzip/zstd-bomb guard).

        httpx auto-decompresses by Content-Encoding even when we request identity, and
        a compression bomb decodes to an unbounded size in one chunk — so the only safe
        move is to refuse a compressed body up front, never streaming/decoding it."""
        decoded = False

        def _mark_decoded():
            nonlocal decoded
            decoded = True

        # Tiny wire body, but declared gzip — a real bomb would balloon on decode.
        resp = _FakeStreamResp(
            b"\x1f\x8b" + b"\x00" * 64,
            headers={"content-encoding": "gzip"},
            on_read=_mark_decoded,
        )
        monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx(lambda url: resp))
        out = await ct.PeerCatalogTransport(["https://1.1.1.1"]).fetch_skills()
        assert out == [] and decoded is False


def test_both_transports_satisfy_protocol():
    assert isinstance(ct.NostrCatalogTransport(), ct.CatalogTransport)
    assert isinstance(ct.PeerCatalogTransport(), ct.CatalogTransport)
