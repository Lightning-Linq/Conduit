"""Catalog transports — fetch remote skill listings as raw Nostr events.

Federation #2, Task 3. A CatalogTransport pulls kind-38383 skill-listing events from
a source — Nostr relays or a federation peer — and returns them UNVERIFIED.
federation_catalog.store_skill_events re-verifies the signature, self-excludes this
node's own listings, and caches them (a source is untrusted for content). This mirrors
the reputation fetch split (federation.fetch_ratings / fetch_from_peers).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

import httpx

from conduit.services.federation import _safe_relays, dedupe_events
from conduit.services.nostr import (
    DEFAULT_RELAYS,
    SKILL_EVENT_KIND,
    NostrEvent,
    _subscribe_across_relays,
)
from conduit.services.url_safety import UnsafeURLError, validate_outbound_url

# Federation peer endpoint that serves a node's local catalog (added in Task 4).
_SKILLS_PATH = "/api/v1/federation/skills"


@runtime_checkable
class CatalogTransport(Protocol):
    """Pulls remote skill-listing events from one source (relay set or peer set)."""

    async def fetch_skills(self, *, since: int = 0, limit: int = 500) -> list[NostrEvent]:
        ...


class NostrCatalogTransport:
    """Pull kind-38383 skill listings from Nostr relays (SSRF-filtered, DNS-pinned).

    Returns RAW events (not the parsed dicts discover_from_relays yields) so the cache
    can re-verify each signature. Unsafe / non-allowlisted relay URLs are dropped
    before any connection; the default skill relays pass without a DNS round-trip.
    """

    def __init__(self, relay_urls: Sequence[str] | None = None, *, timeout: float = 10.0):
        self.relay_urls = list(relay_urls) if relay_urls is not None else list(DEFAULT_RELAYS)
        self.timeout = timeout

    async def fetch_skills(self, *, since: int = 0, limit: int = 500) -> list[NostrEvent]:
        urls = await _safe_relays(self.relay_urls, allowlist=DEFAULT_RELAYS)
        if not urls:
            return []
        filt: dict = {"kinds": [SKILL_EVENT_KIND], "limit": limit}
        if since > 0:
            filt["since"] = since
        return await _subscribe_across_relays(
            [filt], urls, timeout=self.timeout, max_events=limit, pin_dns=True
        )


class PeerCatalogTransport:
    """Pull skill listings from federation peers' serve endpoints (SSRF-pinned).

    HTTP-GETs each peer's /api/v1/federation/skills and parses the returned events.
    Peer URLs are SSRF-validated (https + non-internal host) before any request;
    redirects are disabled; per-peer failures are swallowed. Events are NOT verified
    here — store_skill_events re-verifies (a peer is untrusted for content).
    """

    def __init__(self, peer_urls: Sequence[str] = (), *, timeout: float = 10.0):
        self.peer_urls = list(peer_urls)
        self.timeout = timeout

    async def fetch_skills(self, *, since: int = 0, limit: int = 500) -> list[NostrEvent]:
        collected: list[list[NostrEvent]] = []

        async def _pull_one(base: str) -> None:
            try:
                validate_outbound_url(base)  # https + non-internal host; else skip
            except UnsafeURLError:
                return
            url = base.rstrip("/") + _SKILLS_PATH
            params = {"since": since, "limit": limit}
            try:
                async with httpx.AsyncClient(
                    timeout=self.timeout, follow_redirects=False
                ) as client:
                    resp = await client.get(url, params=params)
                    resp.raise_for_status()
                    raw = resp.json().get("skills", [])
                collected.append([NostrEvent.from_dict(e) for e in raw[:limit]])
            except Exception:
                pass  # per-peer failures are swallowed; a bad peer can't abort the rest

        await asyncio.gather(*[_pull_one(base) for base in self.peer_urls])
        return dedupe_events(collected)
