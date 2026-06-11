"""Batch-4a skills: nostr-profile (websocket) and opentimestamps (calendar).

Both transports are mocked at their single helper so the suite stays offline; the
OTS proof is serialized for real and deserialized back to prove it's valid.
"""

import base64
import hashlib
import json

from app import nostr, ots
from opentimestamps.core.notary import PendingAttestation
from opentimestamps.core.serialize import BytesDeserializationContext
from opentimestamps.core.timestamp import DetachedTimestampFile, Timestamp

_HEX = "82341f882b6eabcd2ba7f1ef90aad961cf074af15b9ef44a09f9d2a8fbfbe6a2"
_NPUB = "npub1sg6plzptd64u62a878hep2kev88swjh3tw00gjsfl8f237lmu63q0uf63m"
_HELLO_SHA = hashlib.sha256(b"hello").hexdigest()


# ---- nostr-profile ----


def test_nostr_profile_found(client, paid, monkeypatch):
    event = {
        "kind": 0,
        "created_at": 1699999999,
        "content": json.dumps({"name": "alice", "lud16": "alice@ln.tips"}),
    }

    async def fake(pubkey):
        return event, "wss://relay.damus.io"

    monkeypatch.setattr(nostr, "fetch_profile_event", fake)
    r = client.post("/skills/nostr-profile", json=paid("nostr-profile", {"pubkey": _HEX}))
    out = r.json()["output"]
    assert out["found"] is True
    assert out["profile"]["lud16"] == "alice@ln.tips"
    assert out["relay"] == "wss://relay.damus.io"


def test_nostr_profile_accepts_npub(client, paid, monkeypatch):
    captured = {}

    async def fake(pubkey):
        captured["pubkey"] = pubkey
        return None, None

    monkeypatch.setattr(nostr, "fetch_profile_event", fake)
    r = client.post("/skills/nostr-profile", json=paid("nostr-profile", {"pubkey": _NPUB}))
    assert r.status_code == 200
    assert captured["pubkey"] == _HEX  # npub decoded to hex before any relay call


def test_nostr_profile_not_found(client, paid, monkeypatch):
    async def fake(pubkey):
        return None, None

    monkeypatch.setattr(nostr, "fetch_profile_event", fake)
    r = client.post("/skills/nostr-profile", json=paid("nostr-profile", {"pubkey": _HEX}))
    out = r.json()["output"]
    assert out["found"] is False
    assert out["profile"] is None


def test_nostr_profile_rejects_bad_pubkey(client, paid):
    r = client.post("/skills/nostr-profile", json=paid("nostr-profile", {"pubkey": "nope"}))
    assert r.status_code == 400


# ---- opentimestamps ----


def _fake_submit(digest):
    timestamp = Timestamp(digest)
    timestamp.attestations.add(PendingAttestation("https://fake.calendar.test"))
    return timestamp


def test_opentimestamps_valid_proof(client, paid, monkeypatch):
    monkeypatch.setattr(ots, "_submit_to_calendars", _fake_submit)
    r = client.post("/skills/opentimestamps", json=paid("opentimestamps", {"sha256": _HELLO_SHA}))
    out = r.json()["output"]
    assert out["sha256"] == _HELLO_SHA
    assert out["status"] == "pending"
    raw = base64.b64decode(out["ots_proof_base64"])
    detached = DetachedTimestampFile.deserialize(BytesDeserializationContext(raw))
    assert detached.file_digest.hex() == _HELLO_SHA  # proof commits exactly our digest


def test_opentimestamps_hashes_data(client, paid, monkeypatch):
    monkeypatch.setattr(ots, "_submit_to_calendars", _fake_submit)
    r = client.post("/skills/opentimestamps", json=paid("opentimestamps", {"data": "hello"}))
    assert r.json()["output"]["sha256"] == _HELLO_SHA


def test_opentimestamps_rejects_bad_hash(client, paid):
    r = client.post("/skills/opentimestamps", json=paid("opentimestamps", {"sha256": "zz"}))
    assert r.status_code == 400


def test_opentimestamps_requires_input(client, paid):
    r = client.post("/skills/opentimestamps", json=paid("opentimestamps", {}))
    assert r.status_code == 400
