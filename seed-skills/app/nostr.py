"""Minimal Nostr client for the nostr-profile skill.

Self-contained NIP-01: connect to a hardcoded relay over websocket, REQ the
author's kind-0 metadata, return the newest one. The relay set is FIXED — user
input is only the pubkey, which goes in the filter, never the connection target —
so there is no user-controlled-host SSRF. ``fetch_profile_event`` is the single
point the tests monkeypatch, keeping the suite offline.

Needs the ``net`` extra (websockets); the loader skips the skill if it is absent.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import websockets

from app.registry import SkillError

# Fixed, well-known public relays, tried in order. Never user-supplied.
_RELAYS = (
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
    "wss://nos.lol",
)
_TIMEOUT = 8.0
_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    generators = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ value
        for i in range(5):
            chk ^= generators[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_decode(bech: str) -> tuple[str, list[int]]:
    if any(ord(x) < 33 or ord(x) > 126 for x in bech):
        raise ValueError("non-printable character")
    if bech.lower() != bech and bech.upper() != bech:
        raise ValueError("mixed case")
    bech = bech.lower()
    pos = bech.rfind("1")
    if pos < 1 or pos + 7 > len(bech):
        raise ValueError("bad separator position")
    if any(x not in _BECH32_CHARSET for x in bech[pos + 1 :]):
        raise ValueError("invalid data character")
    hrp = bech[:pos]
    data = [_BECH32_CHARSET.index(x) for x in bech[pos + 1 :]]
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise ValueError("bad checksum")
    return hrp, data[:-6]


def _convertbits(data: list[int], frombits: int, tobits: int) -> list[int] | None:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    max_acc = (1 << (frombits + tobits - 1)) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = ((acc << frombits) | value) & max_acc
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    # no padding: reject leftover bits (npub must be exactly 32 bytes)
    if bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def normalize_pubkey(raw: str) -> str:
    """Return a 64-char hex pubkey from hex or an npub; raise ValueError if invalid."""
    value = raw.lower()
    if value.startswith("npub1"):
        hrp, data = _bech32_decode(value)
        if hrp != "npub":
            raise ValueError("not an npub")
        decoded = _convertbits(data, 5, 8)
        if decoded is None or len(decoded) != 32:
            raise ValueError("npub does not decode to 32 bytes")
        return bytes(decoded).hex()
    if len(value) == 64 and all(c in "0123456789abcdef" for c in value):
        return value
    raise ValueError("must be 64-char hex or an npub1… string")


async def _query_relay(relay: str, pubkey: str) -> dict | None:
    sub = uuid.uuid4().hex
    req = json.dumps(["REQ", sub, {"kinds": [0], "authors": [pubkey], "limit": 1}])
    newest: dict | None = None
    async with websockets.connect(relay, open_timeout=_TIMEOUT, close_timeout=2) as ws:
        await ws.send(req)
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=_TIMEOUT))
                if not isinstance(msg, list) or len(msg) < 2 or msg[1] != sub:
                    continue
                if msg[0] == "EVENT" and len(msg) >= 3:
                    event = msg[2]
                    if newest is None or event.get("created_at", 0) > newest.get("created_at", 0):
                        newest = event
                elif msg[0] == "EOSE":
                    break
        except TimeoutError:
            pass  # return whatever we collected before the relay went quiet
    return newest


async def fetch_profile_event(pubkey: str) -> tuple[dict | None, str | None]:
    """Newest kind-0 event for ``pubkey`` from the fixed relays.

    Returns (event, relay_url), or (None, None) when the relays are reachable but
    the key has no profile. Raises SkillError only if no relay was reachable.
    """
    queried_ok = False
    for relay in _RELAYS:
        try:
            event = await _query_relay(relay, pubkey)
            queried_ok = True
            if event is not None:
                return event, relay
        except Exception:
            continue  # try the next relay
    if not queried_ok:
        raise SkillError("no Nostr relay reachable")
    return None, None
