"""
Nostr protocol integration for decentralized skill discovery.

Implements NIP-01 (basic protocol), NIP-19 (bech32 encoding), and a custom
event kind (kind 38383) for Conduit skill marketplace listings.

Skills published to Nostr relays are discoverable by any agent on any relay,
removing the dependency on Conduit's centralized PostgreSQL marketplace.

Architecture:
    - NostrKeypair: Generate/load secp256k1 keys, produce x-only pubkeys
    - NostrEvent: Create, serialize, hash, and sign events (BIP-340 schnorr)
    - NostrRelay: Async websocket publish/subscribe to relay servers
    - skill_to_event / event_to_skill: Serialize skills as Nostr events

Event Kind 38383 ("marketplace listing"):
    Content: JSON skill details (name, description, price, schemas)
    Tags:
        ["d", "<skill-id>"]           — deduplication (NIP-33 replaceable)
        ["t", "<category>"]           — category for filtering
        ["t", "<tag>"]                — each tag for filtering
        ["price", "<sats>", "sats"]   — price in satoshis
        ["lightning", "<lnaddr>"]     — provider's Lightning address
        ["endpoint", "<url>"]         — execution endpoint (if any)

Usage:
    from conduit.services.nostr import NostrKeypair, NostrEvent, NostrRelay

    keys = NostrKeypair.generate()
    event = skill_to_event(skill_dict, keys)
    async with NostrRelay("wss://relay.damus.io") as relay:
        ok = await relay.publish(event)
"""

from __future__ import annotations

import hashlib
import json
import secrets
import struct
import time
from dataclasses import dataclass, field
from typing import Any

# --- BIP-340 Schnorr Signatures (via coincurve / libsecp256k1) ---
# C3 fix: replaced pure-Python elliptic curve math with coincurve
# (a thin wrapper around Bitcoin Core's libsecp256k1). This eliminates
# timing side-channels in field arithmetic and is ~100x faster.

try:
    import coincurve
    _HAS_COINCURVE = True
except ImportError:
    import warnings
    warnings.warn(
        "coincurve is not installed — falling back to pure-Python BIP-340 "
        "which is NOT constant-time. Install coincurve for production use: "
        "pip install coincurve>=20.0.0",
        stacklevel=2,
    )
    _HAS_COINCURVE = False

# secp256k1 curve order (still needed for key validation)
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _int_from_bytes(b: bytes) -> int:
    return int.from_bytes(b, "big")


def _xonly_pubkey(privkey_int: int) -> bytes:
    """Derive x-only public key (32 bytes) from private key integer."""
    privkey_bytes = privkey_int.to_bytes(32, "big")
    if _HAS_COINCURVE:
        pk = coincurve.PublicKey.from_secret(privkey_bytes)
        # coincurve format() with compressed=True gives 33 bytes (prefix + x)
        compressed = pk.format(compressed=True)
        return compressed[1:]  # strip the 02/03 prefix to get x-only
    else:
        # Fallback: pure-Python point multiplication (for environments
        # where coincurve can't be installed — dev/CI only, never production)
        return _pure_py_xonly_pubkey(privkey_int)


def _schnorr_sign(msg: bytes, privkey_bytes: bytes, aux_rand: bytes = b"") -> bytes:
    """BIP-340 Schnorr sign. msg must be 32 bytes."""
    assert len(msg) == 32
    if _HAS_COINCURVE:
        pk = coincurve.PrivateKey(privkey_bytes)
        sig = pk.sign_schnorr(msg, aux_randomness=aux_rand if len(aux_rand) == 32 else None)
        assert len(sig) == 64
        return sig
    else:
        return _pure_py_schnorr_sign(msg, privkey_bytes, aux_rand)


def _schnorr_verify(msg: bytes, pubkey_bytes: bytes, sig: bytes) -> bool:
    """BIP-340 Schnorr verify. pubkey is x-only (32 bytes).

    Always uses the pure-Python implementation. This is safe because
    verification only operates on public data — no private key is
    involved, so timing side-channels don't leak secrets. coincurve 21
    doesn't expose a Python-level schnorr_verify anyway.
    """
    assert len(msg) == 32 and len(pubkey_bytes) == 32 and len(sig) == 64
    return _pure_py_schnorr_verify(msg, pubkey_bytes, sig)


# --- Pure-Python fallback (for test/dev environments without coincurve) ---
# WARNING: This code is NOT constant-time and should NOT be used in production.

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_G_X = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_G_Y = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _modinv(a: int, m: int) -> int:
    if a < 0:
        a = a % m
    g, x, _ = _extended_gcd(a, m)
    if g != 1:
        raise ValueError("Modular inverse does not exist")
    return x % m


def _extended_gcd(a: int, b: int) -> tuple[int, int, int]:
    if a == 0:
        return b, 0, 1
    g, x, y = _extended_gcd(b % a, a)
    return g, y - (b // a) * x, x


def _point_add(p1: tuple[int, int] | None, p2: tuple[int, int] | None) -> tuple[int, int] | None:
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and y1 != y2:
        return None
    if x1 == x2:
        lam = (3 * x1 * x1 * _modinv(2 * y1, _P)) % _P
    else:
        lam = ((y2 - y1) * _modinv(x2 - x1, _P)) % _P
    x3 = (lam * lam - x1 - x2) % _P
    y3 = (lam * (x1 - x3) - y1) % _P
    return (x3, y3)


def _point_mul(k: int, point: tuple[int, int] | None = None) -> tuple[int, int] | None:
    if point is None:
        point = (_G_X, _G_Y)
    result: tuple[int, int] | None = None
    addend: tuple[int, int] | None = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _has_even_y(point: tuple[int, int]) -> bool:
    return point[1] % 2 == 0


def _tagged_hash(tag: str, msg: bytes) -> bytes:
    tag_hash = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(tag_hash + tag_hash + msg).digest()


def _bytes_from_int(x: int) -> bytes:
    return x.to_bytes(32, "big")


def _pure_py_xonly_pubkey(privkey_int: int) -> bytes:
    point = _point_mul(privkey_int)
    assert point is not None
    return _bytes_from_int(point[0])


def _pure_py_schnorr_sign(msg: bytes, privkey_bytes: bytes, aux_rand: bytes = b"") -> bytes:
    d0 = _int_from_bytes(privkey_bytes)
    if d0 == 0 or d0 >= N:
        raise ValueError("Invalid private key")
    P_point = _point_mul(d0)
    assert P_point is not None
    d = d0 if _has_even_y(P_point) else N - d0
    if len(aux_rand) == 32:
        t = bytes(a ^ b for a, b in zip(_bytes_from_int(d), _tagged_hash("BIP0340/aux", aux_rand)))
    else:
        t = _bytes_from_int(d)
    k0 = _int_from_bytes(_tagged_hash("BIP0340/nonce", t + _bytes_from_int(P_point[0]) + msg)) % N
    if k0 == 0:
        raise ValueError("Nonce is zero")
    R = _point_mul(k0)
    assert R is not None
    k = k0 if _has_even_y(R) else N - k0
    e = _int_from_bytes(
        _tagged_hash("BIP0340/challenge", _bytes_from_int(R[0]) + _bytes_from_int(P_point[0]) + msg)
    ) % N
    sig = _bytes_from_int(R[0]) + _bytes_from_int((k + e * d) % N)
    assert len(sig) == 64
    return sig


def _pure_py_schnorr_verify(msg: bytes, pubkey_bytes: bytes, sig: bytes) -> bool:
    Px = _int_from_bytes(pubkey_bytes)
    r = _int_from_bytes(sig[:32])
    s = _int_from_bytes(sig[32:])
    if Px >= _P or r >= _P or s >= N:
        return False
    y_sq = (pow(Px, 3, _P) + 7) % _P
    y = pow(y_sq, (_P + 1) // 4, _P)
    if pow(y, 2, _P) != y_sq:
        return False
    if y % 2 != 0:
        y = _P - y
    P_point = (Px, y)
    e = _int_from_bytes(
        _tagged_hash("BIP0340/challenge", sig[:32] + pubkey_bytes + msg)
    ) % N
    sG = _point_mul(s)
    eP = _point_mul(N - e, P_point)
    R = _point_add(sG, eP)
    if R is None or not _has_even_y(R) or R[0] != r:
        return False
    return True


# --- NIP-19: bech32 encoding for npub/nsec/note ---

BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_polymod(values: list[int]) -> int:
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_create_checksum(hrp: str, data: list[int]) -> list[int]:
    values = _bech32_hrp_expand(hrp) + data
    polymod = _bech32_polymod(values + [0, 0, 0, 0, 0, 0]) ^ 1
    return [(polymod >> 5 * (5 - i)) & 31 for i in range(6)]


def _convertbits(data: bytes, frombits: int, tobits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    ret = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if pad:
        if bits:
            ret.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return []
    return ret


def bech32_encode(hrp: str, data: bytes) -> str:
    """Encode bytes as bech32 with given human-readable prefix."""
    dp = _convertbits(data, 8, 5)
    checksum = _bech32_create_checksum(hrp, dp)
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in dp + checksum)


def bech32_decode(bech: str) -> tuple[str, bytes]:
    """Decode a bech32 string. Returns (hrp, data_bytes)."""
    pos = bech.rfind("1")
    if pos < 1:
        raise ValueError("Invalid bech32 string")
    hrp = bech[:pos]
    data_part = bech[pos + 1 :]
    # L1: Validate charset before indexing to avoid cryptic ValueError
    for c in data_part:
        if c not in BECH32_CHARSET:
            raise ValueError(f"Invalid bech32 character: {c!r}")
    data = [BECH32_CHARSET.index(c) for c in data_part]
    if _bech32_polymod(_bech32_hrp_expand(hrp) + data) != 1:
        raise ValueError("Invalid bech32 checksum")
    decoded = _convertbits(bytes(data[:-6]), 5, 8, pad=False)
    return hrp, bytes(decoded)


def npub_encode(pubkey_hex: str) -> str:
    """Encode hex pubkey as npub1..."""
    return bech32_encode("npub", bytes.fromhex(pubkey_hex))


def nsec_encode(privkey_hex: str) -> str:
    """Encode hex private key as nsec1..."""
    return bech32_encode("nsec", bytes.fromhex(privkey_hex))


def npub_decode(npub: str) -> str:
    """Decode npub1... to hex pubkey."""
    hrp, data = bech32_decode(npub)
    if hrp != "npub":
        raise ValueError(f"Expected npub, got {hrp}")
    return data.hex()


def nsec_decode(nsec: str) -> str:
    """Decode nsec1... to hex private key."""
    hrp, data = bech32_decode(nsec)
    if hrp != "nsec":
        raise ValueError(f"Expected nsec, got {hrp}")
    return data.hex()


# --- Nostr Event Kind for Conduit Skills ---
# Using kind 38383 — parameterized replaceable event (NIP-33 range 30000-39999)
# The "d" tag makes it replaceable per skill ID, so re-publishing updates the listing.
SKILL_EVENT_KIND = 38383


@dataclass
class NostrKeypair:
    """A Nostr identity — secp256k1 keypair with x-only pubkey."""

    privkey_hex: str  # 32-byte hex secret
    pubkey_hex: str  # 32-byte x-only hex pubkey

    @classmethod
    def generate(cls) -> NostrKeypair:
        """Generate a fresh random keypair."""
        privkey = secrets.token_bytes(32)
        privkey_int = _int_from_bytes(privkey)
        # Ensure valid scalar (extremely unlikely to fail)
        while privkey_int == 0 or privkey_int >= N:
            privkey = secrets.token_bytes(32)
            privkey_int = _int_from_bytes(privkey)
        pubkey = _xonly_pubkey(privkey_int)
        return cls(privkey_hex=privkey.hex(), pubkey_hex=pubkey.hex())

    @classmethod
    def from_nsec(cls, nsec: str) -> NostrKeypair:
        """Load keypair from an nsec-encoded private key."""
        privkey_hex = nsec_decode(nsec)
        privkey_int = _int_from_bytes(bytes.fromhex(privkey_hex))
        pubkey = _xonly_pubkey(privkey_int)
        return cls(privkey_hex=privkey_hex, pubkey_hex=pubkey.hex())

    @classmethod
    def from_hex(cls, privkey_hex: str) -> NostrKeypair:
        """Load keypair from a hex private key."""
        privkey_int = _int_from_bytes(bytes.fromhex(privkey_hex))
        pubkey = _xonly_pubkey(privkey_int)
        return cls(privkey_hex=privkey_hex, pubkey_hex=pubkey.hex())

    @property
    def npub(self) -> str:
        return npub_encode(self.pubkey_hex)

    @property
    def nsec(self) -> str:
        return nsec_encode(self.privkey_hex)


@dataclass
class NostrEvent:
    """A Nostr event (NIP-01)."""

    pubkey: str = ""
    created_at: int = 0
    kind: int = 1
    tags: list[list[str]] = field(default_factory=list)
    content: str = ""
    id: str = ""
    sig: str = ""

    def serialize_for_id(self) -> str:
        """Canonical JSON serialization for event ID computation (NIP-01)."""
        return json.dumps(
            [0, self.pubkey, self.created_at, self.kind, self.tags, self.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def compute_id(self) -> str:
        """Compute the event ID (SHA-256 of canonical serialization)."""
        serialized = self.serialize_for_id()
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def sign(self, keypair: NostrKeypair) -> None:
        """Sign the event with the given keypair. Sets pubkey, id, and sig."""
        self.pubkey = keypair.pubkey_hex
        if self.created_at == 0:
            self.created_at = int(time.time())
        self.id = self.compute_id()

        msg = bytes.fromhex(self.id)
        privkey = bytes.fromhex(keypair.privkey_hex)
        aux_rand = secrets.token_bytes(32)
        self.sig = _schnorr_sign(msg, privkey, aux_rand).hex()

    def verify(self) -> bool:
        """Verify the event signature."""
        expected_id = self.compute_id()
        if self.id != expected_id:
            return False
        try:
            return _schnorr_verify(
                bytes.fromhex(self.id),
                bytes.fromhex(self.pubkey),
                bytes.fromhex(self.sig),
            )
        except Exception:
            return False

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "id": self.id,
            "pubkey": self.pubkey,
            "created_at": self.created_at,
            "kind": self.kind,
            "tags": self.tags,
            "content": self.content,
            "sig": self.sig,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NostrEvent:
        """Deserialize from dict."""
        return cls(
            pubkey=d.get("pubkey", ""),
            created_at=d.get("created_at", 0),
            kind=d.get("kind", 1),
            tags=d.get("tags", []),
            content=d.get("content", ""),
            id=d.get("id", ""),
            sig=d.get("sig", ""),
        )


# --- Skill <-> Nostr Event Conversion ---


def skill_to_event(skill: dict[str, Any], keypair: NostrKeypair) -> NostrEvent:
    """
    Convert a Conduit skill dict to a signed Nostr event.

    The skill dict should contain the standard fields from the Skill model:
    name, description, category, tags, price_sats, provider_name,
    provider_lightning_address, input_schema, output_schema, etc.
    """
    skill_id = str(skill.get("id", skill.get("skill_id", "")))

    tags = [
        ["d", skill_id],  # NIP-33 replaceable identifier
        ["t", skill.get("category", "general")],
        ["price", str(skill.get("price_sats", 0)), "sats"],
        ["name", skill.get("name", "")],
        ["provider", skill.get("provider_name", "")],
    ]

    # Add Lightning address
    ln_addr = skill.get("provider_lightning_address", "")
    if ln_addr:
        tags.append(["lightning", ln_addr])

    # Add endpoint if present
    endpoint = skill.get("endpoint_url", "")
    if endpoint:
        tags.append(["endpoint", endpoint])

    # Add individual tags for discovery
    skill_tags = skill.get("tags", "")
    if skill_tags:
        for tag in skill_tags.split(","):
            tag = tag.strip()
            if tag:
                tags.append(["t", tag.lower()])

    # Content is the full skill JSON for rich clients
    content = json.dumps(
        {
            "name": skill.get("name", ""),
            "description": skill.get("description", ""),
            "category": skill.get("category", ""),
            "price_sats": skill.get("price_sats", 0),
            "provider_name": skill.get("provider_name", ""),
            "provider_lightning_address": ln_addr,
            "input_schema": skill.get("input_schema"),
            "output_schema": skill.get("output_schema"),
            "endpoint_url": endpoint,
            "conduit_version": "0.1.0",
        },
        separators=(",", ":"),
    )

    event = NostrEvent(
        kind=SKILL_EVENT_KIND,
        tags=tags,
        content=content,
    )
    event.sign(keypair)
    return event


def event_to_skill(event: NostrEvent) -> dict[str, Any] | None:
    """
    Parse a Nostr event back into a Conduit skill dict.
    Returns None if the event isn't a valid skill listing.
    """
    if event.kind != SKILL_EVENT_KIND:
        return None

    try:
        content = json.loads(event.content)
    except (json.JSONDecodeError, TypeError):
        return None

    # Extract skill_id from "d" tag
    skill_id = ""
    for tag in event.tags:
        if len(tag) >= 2 and tag[0] == "d":
            skill_id = tag[1]
            break

    return {
        "id": skill_id,
        "name": content.get("name", ""),
        "description": content.get("description", ""),
        "category": content.get("category", ""),
        "price_sats": content.get("price_sats", 0),
        "provider_name": content.get("provider_name", ""),
        "provider_lightning_address": content.get("provider_lightning_address", ""),
        "provider_pubkey": event.pubkey,
        "input_schema": content.get("input_schema"),
        "output_schema": content.get("output_schema"),
        "endpoint_url": content.get("endpoint_url", ""),
        "nostr_event_id": event.id,
        "nostr_pubkey": event.pubkey,
        "nostr_created_at": event.created_at,
        "nostr_sig": event.sig,
        "source": "nostr",
    }


# --- Nostr Relay Client ---


def build_req_filter(
    kinds: list[int] | None = None,
    authors: list[str] | None = None,
    tags: dict[str, list[str]] | None = None,
    since: int | None = None,
    until: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Build a REQ filter object for relay subscriptions."""
    f: dict[str, Any] = {}
    if kinds:
        f["kinds"] = kinds
    if authors:
        f["authors"] = authors
    if tags:
        for key, values in tags.items():
            f[f"#{key}"] = values
    if since is not None:
        f["since"] = since
    if until is not None:
        f["until"] = until
    if limit is not None:
        f["limit"] = limit
    return f


def build_skill_filter(
    category: str = "",
    max_price_sats: int = 0,
    since_hours: int = 24,
    limit: int = 50,
) -> dict[str, Any]:
    """Build a filter for discovering Conduit skills on Nostr relays."""
    f: dict[str, Any] = {"kinds": [SKILL_EVENT_KIND]}

    if category:
        f["#t"] = [category.lower()]

    if since_hours > 0:
        f["since"] = int(time.time()) - (since_hours * 3600)

    f["limit"] = limit
    return f


class NostrRelay:
    """
    Async Nostr relay client using websockets.

    Usage:
        async with NostrRelay("wss://relay.damus.io") as relay:
            event = skill_to_event(skill, keypair)
            ok = await relay.publish(event)
            skills = await relay.fetch_skills(category="analytics")
    """

    def __init__(self, url: str, timeout: float = 10.0):
        self.url = url
        self.timeout = timeout
        self._ws = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    async def connect(self):
        """Connect to the relay via websocket."""
        try:
            import websockets
        except ImportError:
            raise ImportError(
                "websockets package required: pip install websockets"
            )
        self._ws = await websockets.connect(self.url, close_timeout=self.timeout)

    async def close(self):
        """Close the websocket connection."""
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def publish(self, event: NostrEvent) -> bool:
        """
        Publish an event to the relay.
        Returns True if the relay accepted it (OK message).
        """
        import asyncio

        if not self._ws:
            raise RuntimeError("Not connected to relay")

        msg = json.dumps(["EVENT", event.to_dict()])
        await self._ws.send(msg)

        # Wait for OK response
        try:
            response = await asyncio.wait_for(
                self._ws.recv(), timeout=self.timeout
            )
            data = json.loads(response)
            # ["OK", event_id, true/false, "message"]
            if isinstance(data, list) and len(data) >= 3 and data[0] == "OK":
                return bool(data[2])
        except asyncio.TimeoutError:
            pass
        return False

    async def subscribe(
        self, filters: list[dict[str, Any]], sub_id: str = ""
    ) -> list[NostrEvent]:
        """
        Send a REQ and collect all matching events until EOSE.
        Returns list of NostrEvent objects.
        """
        import asyncio

        if not self._ws:
            raise RuntimeError("Not connected to relay")

        if not sub_id:
            sub_id = secrets.token_hex(8)

        msg = json.dumps(["REQ", sub_id] + filters)
        await self._ws.send(msg)

        events: list[NostrEvent] = []
        try:
            while True:
                response = await asyncio.wait_for(
                    self._ws.recv(), timeout=self.timeout
                )
                data = json.loads(response)

                if isinstance(data, list):
                    if data[0] == "EVENT" and len(data) >= 3:
                        event = NostrEvent.from_dict(data[2])
                        if event.verify():
                            events.append(event)
                    elif data[0] == "EOSE":
                        break
                    elif data[0] == "NOTICE":
                        break
        except asyncio.TimeoutError:
            pass

        # Close subscription
        close_msg = json.dumps(["CLOSE", sub_id])
        try:
            await self._ws.send(close_msg)
        except Exception:
            pass

        return events

    async def fetch_skills(
        self,
        category: str = "",
        max_price_sats: int = 0,
        since_hours: int = 24,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """
        Fetch Conduit skill listings from this relay.
        Returns list of skill dicts parsed from events.
        """
        filt = build_skill_filter(
            category=category,
            max_price_sats=max_price_sats,
            since_hours=since_hours,
            limit=limit,
        )
        events = await self.subscribe([filt])

        skills = []
        for event in events:
            skill = event_to_skill(event)
            if skill:
                # Post-filter by price if requested (relay can't filter by price)
                if max_price_sats > 0 and skill["price_sats"] > max_price_sats:
                    continue
                skills.append(skill)

        return skills


# --- Multi-relay helpers ---


async def publish_to_relays(
    event: NostrEvent, relay_urls: list[str], timeout: float = 10.0
) -> dict[str, bool]:
    """
    Publish an event to multiple relays concurrently.
    Returns {relay_url: success_bool} for each relay.
    """
    import asyncio

    results: dict[str, bool] = {}

    async def _publish_one(url: str):
        try:
            async with NostrRelay(url, timeout=timeout) as relay:
                ok = await relay.publish(event)
                results[url] = ok
        except Exception as e:
            results[url] = False

    await asyncio.gather(*[_publish_one(url) for url in relay_urls])
    return results


async def discover_from_relays(
    relay_urls: list[str],
    category: str = "",
    max_price_sats: int = 0,
    since_hours: int = 24,
    limit: int = 50,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """
    Discover skills across multiple relays concurrently.
    Deduplicates by event ID. Returns merged list of skill dicts.
    """
    import asyncio

    all_skills: dict[str, dict[str, Any]] = {}  # keyed by nostr_event_id

    async def _fetch_one(url: str):
        try:
            async with NostrRelay(url, timeout=timeout) as relay:
                skills = await relay.fetch_skills(
                    category=category,
                    max_price_sats=max_price_sats,
                    since_hours=since_hours,
                    limit=limit,
                )
                for skill in skills:
                    eid = skill.get("nostr_event_id", "")
                    if eid and eid not in all_skills:
                        skill["relay"] = url
                        all_skills[eid] = skill
        except Exception:
            pass

    await asyncio.gather(*[_fetch_one(url) for url in relay_urls])
    return list(all_skills.values())


# --- Default relay list ---

DEFAULT_RELAYS = [
    "wss://relay.damus.io",
    "wss://relay.nostr.band",
    "wss://nos.lol",
    "wss://relay.snort.social",
]
