"""Tests for the shared node Nostr identity (one provider key for both front doors).

Resolution order: env NOSTR_PRIVATE_KEY > persisted credentials/nostr.nsec >
generate + persist. The file fallback (N11) keeps the MCP and REST processes on the
same key when the env var is unset.
"""

import pytest

import conduit.services.node_identity as ni
from conduit.core.config import settings
from conduit.services.nostr import NostrKeypair


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Fresh process cache + a temp key file so tests never touch real credentials/."""
    monkeypatch.setattr(ni, "_node_keys", None)
    monkeypatch.setattr(ni, "_NSEC_FILE", tmp_path / "nostr.nsec")


def test_loads_configured_hex_key(monkeypatch):
    kp = NostrKeypair.generate()
    monkeypatch.setattr(settings, "nostr_private_key", kp.privkey_hex)
    got = ni.get_node_keypair()
    assert got.pubkey_hex == kp.pubkey_hex
    assert ni.get_node_keypair() is got  # cached: same object on repeat


def test_loads_configured_nsec(monkeypatch):
    kp = NostrKeypair.generate()
    monkeypatch.setattr(settings, "nostr_private_key", kp.nsec)
    assert ni.get_node_keypair().pubkey_hex == kp.pubkey_hex
    assert not ni._NSEC_FILE.exists()  # env path writes no file


def test_loads_persisted_file_when_unset(monkeypatch):
    kp = NostrKeypair.generate()
    ni._NSEC_FILE.write_text(kp.nsec + "\n")
    monkeypatch.setattr(settings, "nostr_private_key", "")
    assert ni.get_node_keypair().pubkey_hex == kp.pubkey_hex


def test_generates_persists_and_converges(monkeypatch):
    monkeypatch.setattr(settings, "nostr_private_key", "")
    first = ni.get_node_keypair()
    assert len(first.pubkey_hex) == 64
    assert ni._NSEC_FILE.exists()  # persisted so other processes reuse it

    # Second process: clear the cache; the same file yields the same key (no desync).
    monkeypatch.setattr(ni, "_node_keys", None)
    assert ni.get_node_keypair().pubkey_hex == first.pubkey_hex
