"""
Tests for Conduit Nostr integration — decentralized skill discovery.

Covers:
- BIP-340 Schnorr signatures (sign, verify, tamper detection)
- NIP-19 bech32 encoding (npub, nsec round-trips)
- Nostr event creation, serialization, ID computation
- Skill-to-event and event-to-skill conversion
- Filter construction for relay queries
- Keypair generation and loading
- Event kind correctness
"""

import hashlib
import json
import time

import pytest

from conduit.services.nostr import (
    SKILL_EVENT_KIND,
    NostrEvent,
    NostrKeypair,
    NostrRelay,
    _bytes_from_int,
    _int_from_bytes,
    _schnorr_sign,
    _schnorr_verify,
    _subscribe_across_relays,
    _tagged_hash,
    _xonly_pubkey,
    bech32_decode,
    build_req_filter,
    build_skill_filter,
    event_to_skill,
    npub_decode,
    npub_encode,
    nsec_decode,
    nsec_encode,
    skill_to_event,
)

# =============================================================================
# Test Fixtures
# =============================================================================


SAMPLE_SKILL = {
    "id": "550e8400-e29b-41d4-a716-446655440000",
    "name": "Bitcoin Price Analysis",
    "description": "Analyzes BTC price action and on-chain metrics.",
    "category": "analytics",
    "tags": "bitcoin,price,onchain",
    "price_sats": 100,
    "provider_name": "ChainSight",
    "provider_lightning_address": "chainsight@getalby.com",
    "input_schema": {"type": "object", "properties": {"timeframe": {"type": "string"}}},
    "output_schema": {"type": "object", "properties": {"summary": {"type": "string"}}},
    "endpoint_url": "https://api.chainsight.com/execute",
}


@pytest.fixture
def keypair():
    """Generate a fresh keypair for testing."""
    return NostrKeypair.generate()


@pytest.fixture
def keypair2():
    """A second keypair for cross-key tests."""
    return NostrKeypair.generate()


# =============================================================================
# BIP-340 Schnorr Signature Tests
# =============================================================================


class TestSchnorrSignatures:
    """Test BIP-340 Schnorr sign and verify."""

    def test_sign_and_verify(self, keypair):
        msg = hashlib.sha256(b"test message").digest()
        privkey = bytes.fromhex(keypair.privkey_hex)
        pubkey = bytes.fromhex(keypair.pubkey_hex)
        sig = _schnorr_sign(msg, privkey)
        assert len(sig) == 64
        assert _schnorr_verify(msg, pubkey, sig)

    def test_wrong_message_fails(self, keypair):
        msg1 = hashlib.sha256(b"message 1").digest()
        msg2 = hashlib.sha256(b"message 2").digest()
        privkey = bytes.fromhex(keypair.privkey_hex)
        pubkey = bytes.fromhex(keypair.pubkey_hex)
        sig = _schnorr_sign(msg1, privkey)
        assert not _schnorr_verify(msg2, pubkey, sig)

    def test_wrong_pubkey_fails(self, keypair, keypair2):
        msg = hashlib.sha256(b"test").digest()
        privkey = bytes.fromhex(keypair.privkey_hex)
        wrong_pubkey = bytes.fromhex(keypair2.pubkey_hex)
        sig = _schnorr_sign(msg, privkey)
        assert not _schnorr_verify(msg, wrong_pubkey, sig)

    def test_tampered_signature_fails(self, keypair):
        msg = hashlib.sha256(b"test").digest()
        privkey = bytes.fromhex(keypair.privkey_hex)
        pubkey = bytes.fromhex(keypair.pubkey_hex)
        sig = _schnorr_sign(msg, privkey)
        # Flip a byte in the signature
        tampered = bytearray(sig)
        tampered[0] ^= 0xFF
        assert not _schnorr_verify(msg, pubkey, bytes(tampered))

    def test_deterministic_with_same_aux(self, keypair):
        """Same aux_rand produces same signature (deterministic nonce)."""
        msg = hashlib.sha256(b"deterministic test").digest()
        privkey = bytes.fromhex(keypair.privkey_hex)
        aux = b"\x00" * 32
        sig1 = _schnorr_sign(msg, privkey, aux)
        sig2 = _schnorr_sign(msg, privkey, aux)
        assert sig1 == sig2

    def test_different_aux_different_sig(self, keypair):
        """Different aux_rand produces different signature."""
        msg = hashlib.sha256(b"nonce test").digest()
        privkey = bytes.fromhex(keypair.privkey_hex)
        sig1 = _schnorr_sign(msg, privkey, b"\x00" * 32)
        sig2 = _schnorr_sign(msg, privkey, b"\x01" * 32)
        assert sig1 != sig2
        # Both should still verify
        pubkey = bytes.fromhex(keypair.pubkey_hex)
        assert _schnorr_verify(msg, pubkey, sig1)
        assert _schnorr_verify(msg, pubkey, sig2)


# =============================================================================
# NIP-19 Bech32 Encoding Tests
# =============================================================================


class TestNIP19:
    """Test NIP-19 bech32 encoding for npub and nsec."""

    def test_npub_roundtrip(self, keypair):
        npub = npub_encode(keypair.pubkey_hex)
        assert npub.startswith("npub1")
        decoded = npub_decode(npub)
        assert decoded == keypair.pubkey_hex

    def test_nsec_roundtrip(self, keypair):
        nsec = nsec_encode(keypair.privkey_hex)
        assert nsec.startswith("nsec1")
        decoded = nsec_decode(nsec)
        assert decoded == keypair.privkey_hex

    def test_npub_decode_wrong_prefix_raises(self, keypair):
        nsec = nsec_encode(keypair.privkey_hex)
        with pytest.raises(ValueError, match="Expected npub"):
            npub_decode(nsec)

    def test_nsec_decode_wrong_prefix_raises(self, keypair):
        npub = npub_encode(keypair.pubkey_hex)
        with pytest.raises(ValueError, match="Expected nsec"):
            nsec_decode(npub)

    def test_invalid_bech32_raises(self):
        with pytest.raises((ValueError, IndexError)):
            bech32_decode("not_a_bech32_string")

    def test_keypair_npub_nsec_properties(self, keypair):
        """Keypair .npub and .nsec properties work."""
        assert keypair.npub.startswith("npub1")
        assert keypair.nsec.startswith("nsec1")
        assert npub_decode(keypair.npub) == keypair.pubkey_hex
        assert nsec_decode(keypair.nsec) == keypair.privkey_hex


# =============================================================================
# Nostr Event Tests
# =============================================================================


class TestNostrEvent:
    """Test Nostr event creation, signing, and verification."""

    def test_event_id_is_sha256_of_canonical(self, keypair):
        event = NostrEvent(
            kind=1,
            content="Hello Nostr!",
            created_at=1700000000,
        )
        event.pubkey = keypair.pubkey_hex
        serialized = event.serialize_for_id()
        expected_id = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        assert event.compute_id() == expected_id

    def test_canonical_serialization_format(self, keypair):
        """Canonical form is [0, pubkey, created_at, kind, tags, content]."""
        event = NostrEvent(
            kind=1,
            content="test",
            tags=[["t", "hello"]],
            created_at=1700000000,
        )
        event.pubkey = keypair.pubkey_hex
        serialized = json.loads(event.serialize_for_id())
        assert serialized[0] == 0
        assert serialized[1] == keypair.pubkey_hex
        assert serialized[2] == 1700000000
        assert serialized[3] == 1
        assert serialized[4] == [["t", "hello"]]
        assert serialized[5] == "test"

    def test_sign_sets_fields(self, keypair):
        event = NostrEvent(kind=1, content="test")
        event.sign(keypair)
        assert event.pubkey == keypair.pubkey_hex
        assert event.id != ""
        assert event.sig != ""
        assert event.created_at > 0

    def test_signed_event_verifies(self, keypair):
        event = NostrEvent(kind=1, content="test")
        event.sign(keypair)
        assert event.verify()

    def test_tampered_content_fails_verify(self, keypair):
        event = NostrEvent(kind=1, content="original")
        event.sign(keypair)
        event.content = "tampered"
        assert not event.verify()

    def test_tampered_tags_fails_verify(self, keypair):
        event = NostrEvent(kind=1, content="test", tags=[["t", "a"]])
        event.sign(keypair)
        event.tags = [["t", "b"]]
        assert not event.verify()

    def test_roundtrip_dict(self, keypair):
        event = NostrEvent(kind=1, content="roundtrip test", tags=[["t", "test"]])
        event.sign(keypair)
        d = event.to_dict()
        event2 = NostrEvent.from_dict(d)
        assert event2.id == event.id
        assert event2.sig == event.sig
        assert event2.content == event.content
        assert event2.verify()


# =============================================================================
# Keypair Loading Tests
# =============================================================================


class TestKeypairLoading:
    """Test various ways to load/create keypairs."""

    def test_generate_unique(self):
        k1 = NostrKeypair.generate()
        k2 = NostrKeypair.generate()
        assert k1.pubkey_hex != k2.pubkey_hex
        assert k1.privkey_hex != k2.privkey_hex

    def test_from_hex(self, keypair):
        loaded = NostrKeypair.from_hex(keypair.privkey_hex)
        assert loaded.pubkey_hex == keypair.pubkey_hex
        assert loaded.privkey_hex == keypair.privkey_hex

    def test_from_nsec(self, keypair):
        loaded = NostrKeypair.from_nsec(keypair.nsec)
        assert loaded.pubkey_hex == keypair.pubkey_hex

    def test_pubkey_is_32_bytes_hex(self, keypair):
        assert len(keypair.pubkey_hex) == 64  # 32 bytes * 2 hex chars
        bytes.fromhex(keypair.pubkey_hex)  # should not raise

    def test_privkey_is_32_bytes_hex(self, keypair):
        assert len(keypair.privkey_hex) == 64
        bytes.fromhex(keypair.privkey_hex)


# =============================================================================
# Skill <-> Event Conversion Tests
# =============================================================================


class TestSkillEventConversion:
    """Test converting skills to/from Nostr events."""

    def test_skill_to_event_kind(self, keypair):
        event = skill_to_event(SAMPLE_SKILL, keypair)
        assert event.kind == SKILL_EVENT_KIND
        assert event.kind == 38383

    def test_skill_to_event_has_d_tag(self, keypair):
        event = skill_to_event(SAMPLE_SKILL, keypair)
        d_tags = [t for t in event.tags if t[0] == "d"]
        assert len(d_tags) == 1
        assert d_tags[0][1] == SAMPLE_SKILL["id"]

    def test_skill_to_event_has_price_tag(self, keypair):
        event = skill_to_event(SAMPLE_SKILL, keypair)
        price_tags = [t for t in event.tags if t[0] == "price"]
        assert len(price_tags) == 1
        assert price_tags[0][1] == "100"
        assert price_tags[0][2] == "sats"

    def test_skill_to_event_has_category_tag(self, keypair):
        event = skill_to_event(SAMPLE_SKILL, keypair)
        t_tags = [t for t in event.tags if t[0] == "t"]
        categories = [t[1] for t in t_tags]
        assert "analytics" in categories

    def test_skill_to_event_has_lightning_tag(self, keypair):
        event = skill_to_event(SAMPLE_SKILL, keypair)
        ln_tags = [t for t in event.tags if t[0] == "lightning"]
        assert len(ln_tags) == 1
        assert ln_tags[0][1] == "chainsight@getalby.com"

    def test_skill_to_event_has_endpoint_tag(self, keypair):
        event = skill_to_event(SAMPLE_SKILL, keypair)
        ep_tags = [t for t in event.tags if t[0] == "endpoint"]
        assert len(ep_tags) == 1
        assert ep_tags[0][1] == "https://api.chainsight.com/execute"

    def test_skill_to_event_content_is_json(self, keypair):
        event = skill_to_event(SAMPLE_SKILL, keypair)
        content = json.loads(event.content)
        assert content["name"] == "Bitcoin Price Analysis"
        assert content["price_sats"] == 100
        assert content["conduit_version"] == "0.1.0"

    def test_skill_to_event_is_signed(self, keypair):
        event = skill_to_event(SAMPLE_SKILL, keypair)
        assert event.verify()
        assert event.pubkey == keypair.pubkey_hex

    def test_event_to_skill_roundtrip(self, keypair):
        event = skill_to_event(SAMPLE_SKILL, keypair)
        parsed = event_to_skill(event)
        assert parsed is not None
        assert parsed["name"] == "Bitcoin Price Analysis"
        assert parsed["description"] == "Analyzes BTC price action and on-chain metrics."
        assert parsed["category"] == "analytics"
        assert parsed["price_sats"] == 100
        assert parsed["provider_name"] == "ChainSight"
        assert parsed["provider_lightning_address"] == "chainsight@getalby.com"
        assert parsed["nostr_pubkey"] == keypair.pubkey_hex
        assert parsed["nostr_event_id"] == event.id
        assert parsed["source"] == "nostr"

    def test_event_to_skill_wrong_kind_returns_none(self, keypair):
        event = NostrEvent(kind=1, content="{}")
        event.sign(keypair)
        assert event_to_skill(event) is None

    def test_event_to_skill_invalid_json_returns_none(self, keypair):
        event = NostrEvent(kind=SKILL_EVENT_KIND, content="not json")
        event.sign(keypair)
        assert event_to_skill(event) is None

    def test_skill_without_optional_fields(self, keypair):
        """Minimal skill dict still converts."""
        minimal = {
            "id": "abc-123",
            "name": "Test Skill",
            "description": "A test",
            "category": "test",
            "price_sats": 10,
            "provider_name": "Tester",
        }
        event = skill_to_event(minimal, keypair)
        assert event.verify()
        parsed = event_to_skill(event)
        assert parsed is not None
        assert parsed["name"] == "Test Skill"

    def test_individual_tags_expanded(self, keypair):
        """Comma-separated tags are expanded into individual 't' tags."""
        event = skill_to_event(SAMPLE_SKILL, keypair)
        t_tags = [t[1] for t in event.tags if t[0] == "t"]
        assert "bitcoin" in t_tags
        assert "price" in t_tags
        assert "onchain" in t_tags


# =============================================================================
# Filter Construction Tests
# =============================================================================


class TestFilters:
    """Test Nostr REQ filter construction."""

    def test_skill_filter_has_correct_kind(self):
        f = build_skill_filter()
        assert f["kinds"] == [SKILL_EVENT_KIND]

    def test_skill_filter_with_category(self):
        f = build_skill_filter(category="analytics")
        assert f["#t"] == ["analytics"]

    def test_skill_filter_has_since(self):
        f = build_skill_filter(since_hours=24)
        assert "since" in f
        assert f["since"] > 0
        assert f["since"] < int(time.time())

    def test_skill_filter_has_limit(self):
        f = build_skill_filter(limit=10)
        assert f["limit"] == 10

    def test_generic_req_filter(self):
        f = build_req_filter(
            kinds=[1],
            authors=["abc123"],
            tags={"t": ["bitcoin"]},
            limit=5,
        )
        assert f["kinds"] == [1]
        assert f["authors"] == ["abc123"]
        assert f["#t"] == ["bitcoin"]
        assert f["limit"] == 5

    def test_empty_filter(self):
        f = build_req_filter()
        assert f == {}


# =============================================================================
# Crypto Utility Tests
# =============================================================================


class TestCryptoUtils:
    """Test low-level crypto utilities."""

    def test_tagged_hash_deterministic(self):
        h1 = _tagged_hash("BIP0340/challenge", b"test")
        h2 = _tagged_hash("BIP0340/challenge", b"test")
        assert h1 == h2
        assert len(h1) == 32

    def test_tagged_hash_different_tags(self):
        h1 = _tagged_hash("tag1", b"data")
        h2 = _tagged_hash("tag2", b"data")
        assert h1 != h2

    def test_int_bytes_roundtrip(self):
        for val in [0, 1, 42, 2**256 - 1]:
            assert _int_from_bytes(_bytes_from_int(val)) == val

    def test_xonly_pubkey_is_32_bytes(self, keypair):
        privkey_int = _int_from_bytes(bytes.fromhex(keypair.privkey_hex))
        pubkey = _xonly_pubkey(privkey_int)
        assert len(pubkey) == 32


class _FakeWS:
    """Minimal fake websocket: replays a queue of frames, then hangs.

    Hanging (rather than closing) after the queue drains means only the
    client-side cap or the recv timeout can end subscribe()'s loop — exactly
    what the cap tests need to observe.
    """

    def __init__(self, frames):
        self._frames = list(frames)
        self.recv_count = 0
        self.sent: list[str] = []

    async def send(self, msg):
        self.sent.append(msg)

    async def recv(self):
        self.recv_count += 1
        if self._frames:
            return self._frames.pop(0)
        import asyncio

        await asyncio.sleep(3600)

    async def close(self):
        pass


def _event_frame(event: NostrEvent) -> str:
    return json.dumps(["EVENT", "sub", event.to_dict()])


class TestSubscribeCap:
    """NostrRelay.subscribe max_events — bound memory/CPU vs a flooding relay."""

    async def test_caps_valid_events(self, keypair):
        ev = NostrEvent(kind=1, tags=[], content="hi")
        ev.sign(keypair)
        relay = NostrRelay("wss://x", timeout=0.3)
        relay._ws = _FakeWS([_event_frame(ev)] * 10)  # relay ignores the limit
        out = await relay.subscribe([{}], max_events=3)
        assert len(out) == 3
        assert relay._ws.recv_count == 3  # stopped reading after 3, didn't drain all 10

    async def test_cap_counts_invalid_events(self, keypair):
        # Invalid events count toward the cap too, so an invalid-event flood
        # can't make us verify an unbounded number of signatures.
        ev = NostrEvent(kind=1, tags=[], content="hi")
        ev.sign(keypair)
        d = ev.to_dict()
        d["sig"] = "00" * 64  # fails verify -> never appended
        relay = NostrRelay("wss://x", timeout=0.3)
        relay._ws = _FakeWS([json.dumps(["EVENT", "sub", d])] * 10)
        out = await relay.subscribe([{}], max_events=4)
        assert out == []                  # none valid
        assert relay._ws.recv_count == 4  # but work was still bounded at 4

    async def test_eose_ends_before_cap(self, keypair):
        ev = NostrEvent(kind=1, tags=[], content="hi")
        ev.sign(keypair)
        relay = NostrRelay("wss://x", timeout=0.3)
        relay._ws = _FakeWS([_event_frame(ev), json.dumps(["EOSE", "sub"])])
        out = await relay.subscribe([{}], max_events=100)
        assert len(out) == 1  # EOSE ended it well under the cap


class TestSubscribeAcrossRelays:
    """_subscribe_across_relays — the shared concurrent fan-out behind fetch_ratings."""

    async def test_merges_dedupes_and_forwards_cap(self, monkeypatch, keypair):
        import conduit.services.nostr as nostr_mod

        e1 = NostrEvent(kind=1, tags=[["d", "1"]], content="a")
        e1.sign(keypair)
        e2 = NostrEvent(kind=1, tags=[["d", "2"]], content="b")
        e2.sign(keypair)
        canned = {"wss://x": [e1, e2], "wss://y": [e2]}  # e2 on both relays
        seen_caps: list[int | None] = []

        class FakeRelay:
            def __init__(self, url, timeout=10.0, pin_dns=False):
                self.url = url

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def subscribe(self, filters, sub_id="", max_events=None):
                seen_caps.append(max_events)
                return canned.get(self.url, [])

        monkeypatch.setattr(nostr_mod, "NostrRelay", FakeRelay)
        out = await _subscribe_across_relays([{}], ["wss://x", "wss://y"], max_events=50)
        assert {e.id for e in out} == {e1.id, e2.id}  # deduped across relays
        assert seen_caps == [50, 50]                  # cap forwarded to every relay

    async def test_swallows_relay_errors(self, monkeypatch, keypair):
        import conduit.services.nostr as nostr_mod

        e1 = NostrEvent(kind=1, tags=[], content="a")
        e1.sign(keypair)

        class FakeRelay:
            def __init__(self, url, timeout=10.0, pin_dns=False):
                self.url = url

            async def __aenter__(self):
                if self.url == "wss://bad":
                    raise OSError("boom")
                return self

            async def __aexit__(self, *a):
                return False

            async def subscribe(self, filters, sub_id="", max_events=None):
                return [e1]

        monkeypatch.setattr(nostr_mod, "NostrRelay", FakeRelay)
        out = await _subscribe_across_relays([{}], ["wss://bad", "wss://good"])
        assert {e.id for e in out} == {e1.id}  # dead relay contributes nothing
