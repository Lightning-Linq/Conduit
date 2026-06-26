"""Catalog transports — fetch remote skill listings as raw Nostr events.

Federation #2, Task 3. A CatalogTransport pulls kind-38383 skill-listing events from
a source — Nostr relays or a federation peer — and returns them UNVERIFIED.
federation_catalog.store_skill_events re-verifies the signature, self-excludes this
node's own listings, and caches them (a source is untrusted for content). This mirrors
the reputation fetch split (federation.fetch_ratings / fetch_from_peers).
"""

from __future__ import annotations

import asyncio
import json
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

# Hard ceiling on a single peer's catalog response body. A peer is untrusted
# infrastructure: without a cap, resp.json() would buffer an arbitrarily large
# (or never-ending) body into memory. 8 MiB comfortably holds a full 500-skill
# catalog of even richly-described listings; anything larger means the peer is
# skipped (its events are simply absent — other sources still contribute).
_MAX_PEER_RESPONSE_BYTES = 8 * 1024 * 1024


async def _read_body_capped(resp: httpx.Response, max_bytes: int) -> bytes:
    """Read a streaming response body, refusing one larger than ``max_bytes``.

    httpx has no built-in max-response-size, so we enforce it ourselves. The
    advertised Content-Length is a cheap early-out for an honest oversize body;
    the running byte-count while streaming is the real guard, since a hostile
    peer can omit/understate Content-Length or use chunked transfer-encoding.
    Raises ValueError on exceed — the caller's per-peer ``except`` then skips the
    peer instead of buffering an unbounded body.

    Refuses a compressed body up front: httpx auto-decompresses by the response's
    Content-Encoding (regardless of the Accept-Encoding we sent), and a gzip/br/
    zstd bomb decodes to an unbounded size in a SINGLE aiter_bytes() chunk —
    before any running byte-count can trip. We request identity, so a compliant
    peer never compresses; anything that still declares an encoding is skipped
    without decoding. With no encoding, aiter_bytes() == wire bytes and the cap
    below is exact (overshoot bounded by one network read).
    """
    encoding = (resp.headers.get("content-encoding") or "").strip().lower()
    if encoding and encoding != "identity":
        raise ValueError(f"peer catalog body is {encoding}-encoded; refusing to decode (DoS guard)")
    declared = resp.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        raise ValueError(f"peer catalog Content-Length {declared} exceeds {max_bytes} bytes")
    chunks = bytearray()
    async for chunk in resp.aiter_bytes():
        chunks += chunk
        if len(chunks) > max_bytes:
            raise ValueError(f"peer catalog body exceeded {max_bytes} bytes")
    return bytes(chunks)


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
    redirects are disabled; the response body is streamed under a size cap
    (``max_response_bytes``) so a hostile peer can't exhaust memory; per-peer
    failures are swallowed. Events are NOT verified here — store_skill_events
    re-verifies (a peer is untrusted for content).
    """

    def __init__(
        self,
        peer_urls: Sequence[str] = (),
        *,
        timeout: float = 10.0,
        max_response_bytes: int = _MAX_PEER_RESPONSE_BYTES,
    ):
        self.peer_urls = list(peer_urls)
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes

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
                    # Ask for an uncompressed body so the size cap counts wire bytes
                    # (a compressed body would be refused by _read_body_capped).
                    async with client.stream(
                        "GET", url, params=params, headers={"Accept-Encoding": "identity"}
                    ) as resp:
                        resp.raise_for_status()
                        body = await _read_body_capped(resp, self.max_response_bytes)
                raw = json.loads(body).get("skills", [])
                collected.append([NostrEvent.from_dict(e) for e in raw[:limit]])
            except Exception:
                pass  # per-peer failures are swallowed; a bad peer can't abort the rest

        await asyncio.gather(*[_pull_one(base) for base in self.peer_urls])
        return dedupe_events(collected)
