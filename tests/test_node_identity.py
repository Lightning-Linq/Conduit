"""Tests for the shared node Nostr identity (one provider key for both front doors)."""

import conduit.services.node_identity as ni
from conduit.core.config import settings
from conduit.services.nostr import NostrKeypair


def test_loads_configured_hex_key(monkeypatch):
    kp = NostrKeypair.generate()
    monkeypatch.setattr(settings, "nostr_private_key", kp.privkey_hex)
    monkeypatch.setattr(ni, "_node_keys", None)
    got = ni.get_node_keypair()
    assert got.pubkey_hex == kp.pubkey_hex
    assert ni.get_node_keypair() is got  # cached: same object on repeat


def test_loads_configured_nsec(monkeypatch):
    kp = NostrKeypair.generate()
    monkeypatch.setattr(settings, "nostr_private_key", kp.nsec)
    monkeypatch.setattr(ni, "_node_keys", None)
    assert ni.get_node_keypair().pubkey_hex == kp.pubkey_hex


def test_generates_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "nostr_private_key", "")
    monkeypatch.setattr(ni, "_node_keys", None)
    k1 = ni.get_node_keypair()
    assert len(k1.pubkey_hex) == 64
    assert ni.get_node_keypair() is k1  # still cached
