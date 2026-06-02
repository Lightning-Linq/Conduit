"""Tests for the NWC (Nostr Wallet Connect) backend.

Tests cover:
  - URI parsing (valid, invalid, missing fields)
  - NIP-04 encryption/decryption round-trip
  - NIP-44 v2 encryption: official test vectors + round-trip/tamper/auto-detect
  - Wallet backend interface compliance
  - BOLT-11 amount parsing
  - Error handling
"""

import base64
import hashlib
import json
import secrets
from pathlib import Path
from unittest.mock import patch

import pytest

from conduit.services.nwc import (
    NwcError,
    NwcWalletBackend,
    _derive_pubkey_from_secret,
    _parse_bolt11_amount,
    parse_nwc_uri,
)
from conduit.services.wallet_backend import (
    InvoiceResponse,
    NodeInfo,
    PaymentResponse,
    WalletBackend,
)

# ── Test Data ────────────────────────────────────────────────────────

# Generate a valid keypair for testing. The wallet pubkey must be a real
# secp256k1 x-only point (random bytes are only a valid point ~50% of the
# time), otherwise ECDH against it fails to parse.
TEST_SECRET = secrets.token_hex(32)
TEST_WALLET_PUBKEY = _derive_pubkey_from_secret(secrets.token_hex(32))
TEST_RELAY = "wss://relay.example.com"

VALID_URI = (
    f"nostr+walletconnect://{TEST_WALLET_PUBKEY}"
    f"?relay={TEST_RELAY}&secret={TEST_SECRET}"
)

VALID_URI_WITH_LUD16 = (
    f"nostr+walletconnect://{TEST_WALLET_PUBKEY}"
    f"?relay={TEST_RELAY}&secret={TEST_SECRET}&lud16=test@getalby.com"
)

VALID_URI_MULTI_RELAY = (
    f"nostr+walletconnect://{TEST_WALLET_PUBKEY}"
    f"?relay={TEST_RELAY}&relay=wss://relay2.example.com&secret={TEST_SECRET}"
)


# ── URI Parsing ──────────────────────────────────────────────────────


class TestNwcUriParsing:
    """Tests for parse_nwc_uri."""

    def test_valid_uri(self):
        conn = parse_nwc_uri(VALID_URI)
        assert conn.wallet_pubkey == TEST_WALLET_PUBKEY
        assert conn.client_secret == TEST_SECRET
        assert conn.relays == [TEST_RELAY]
        assert conn.lud16 == ""

    def test_valid_uri_with_lud16(self):
        conn = parse_nwc_uri(VALID_URI_WITH_LUD16)
        assert conn.lud16 == "test@getalby.com"

    def test_valid_uri_multi_relay(self):
        conn = parse_nwc_uri(VALID_URI_MULTI_RELAY)
        assert len(conn.relays) == 2
        assert "wss://relay2.example.com" in conn.relays

    def test_invalid_scheme(self):
        with pytest.raises(ValueError, match="must start with nostr\\+walletconnect://"):
            parse_nwc_uri("https://example.com")

    def test_missing_pubkey(self):
        with pytest.raises(ValueError, match="wallet pubkey"):
            parse_nwc_uri("nostr+walletconnect://tooshort?relay=wss://r.com&secret=" + "a" * 64)

    def test_missing_secret(self):
        with pytest.raises(ValueError, match="secret"):
            parse_nwc_uri(f"nostr+walletconnect://{'a' * 64}?relay=wss://r.com")

    def test_missing_relay(self):
        with pytest.raises(ValueError, match="relay"):
            parse_nwc_uri(f"nostr+walletconnect://{'a' * 64}?secret={'b' * 64}")

    def test_short_secret(self):
        with pytest.raises(ValueError, match="secret"):
            parse_nwc_uri(f"nostr+walletconnect://{'a' * 64}?relay=wss://r.com&secret=tooshort")


# ── Pubkey Derivation ────────────────────────────────────────────────


class TestPubkeyDerivation:
    """Tests for _derive_pubkey_from_secret."""

    def test_derives_32_byte_hex(self):
        pubkey = _derive_pubkey_from_secret(TEST_SECRET)
        assert len(pubkey) == 64  # 32 bytes hex
        bytes.fromhex(pubkey)  # Should not raise

    def test_deterministic(self):
        pk1 = _derive_pubkey_from_secret(TEST_SECRET)
        pk2 = _derive_pubkey_from_secret(TEST_SECRET)
        assert pk1 == pk2

    def test_different_secrets_different_pubkeys(self):
        pk1 = _derive_pubkey_from_secret(TEST_SECRET)
        pk2 = _derive_pubkey_from_secret(secrets.token_hex(32))
        assert pk1 != pk2


# ── BOLT-11 Amount Parsing ───────────────────────────────────────────


class TestBolt11AmountParsing:
    """Tests for _parse_bolt11_amount."""

    def test_milli_btc(self):
        # 1m = 0.001 BTC = 100,000 sats
        assert _parse_bolt11_amount("lnbc1m1rest") == 100_000

    def test_micro_btc(self):
        # 1u = 0.000001 BTC = 100 sats
        assert _parse_bolt11_amount("lnbc1u1rest") == 100

    def test_nano_btc(self):
        # 500n = 50 sats
        assert _parse_bolt11_amount("lnbc500n1rest") == 50

    def test_whole_btc(self):
        # 1 BTC = 100,000,000 sats
        assert _parse_bolt11_amount("lnbc11rest") == 100_000_000

    def test_testnet_prefix(self):
        assert _parse_bolt11_amount("lntb1u1rest") == 100

    def test_regtest_prefix(self):
        assert _parse_bolt11_amount("lnbcrt1u1rest") == 100

    def test_no_amount(self):
        # No amount before separator
        assert _parse_bolt11_amount("lnbc1rest") == 0

    def test_invalid_prefix(self):
        assert _parse_bolt11_amount("invalid1rest") == 0

    def test_empty_string(self):
        assert _parse_bolt11_amount("") == 0


# ── NIP-04 Encryption ────────────────────────────────────────────────


class TestNip04Encryption:
    """Tests for NIP-04 encrypt/decrypt round-trip."""

    def test_encrypt_decrypt_round_trip(self):
        backend = NwcWalletBackend(VALID_URI)
        plaintext = '{"method": "get_balance", "params": {}}'

        encrypted = backend._nip04_encrypt(plaintext, TEST_WALLET_PUBKEY)
        assert "?iv=" in encrypted
        assert encrypted != plaintext

        # Decrypt with same shared secret
        decrypted = backend._nip04_decrypt(encrypted, TEST_WALLET_PUBKEY)
        assert decrypted == plaintext

    def test_encrypt_produces_different_output(self):
        """Two encryptions of the same plaintext should differ (random IV)."""
        backend = NwcWalletBackend(VALID_URI)
        plaintext = "test message"

        enc1 = backend._nip04_encrypt(plaintext, TEST_WALLET_PUBKEY)
        enc2 = backend._nip04_encrypt(plaintext, TEST_WALLET_PUBKEY)
        assert enc1 != enc2  # Different IVs

    def test_decrypt_invalid_format(self):
        backend = NwcWalletBackend(VALID_URI)
        with pytest.raises(NwcError, match="Invalid NIP-04"):
            backend._nip04_decrypt("no-iv-separator-here", TEST_WALLET_PUBKEY)


# ── Wallet Backend Interface ────────────────────────────────────────


class TestNwcWalletBackendInterface:
    """Tests that NwcWalletBackend satisfies the WalletBackend protocol."""

    def test_implements_protocol(self):
        backend = NwcWalletBackend(VALID_URI)
        assert isinstance(backend, WalletBackend)

    def test_connect_disconnect(self):
        backend = NwcWalletBackend(VALID_URI)
        assert not backend.is_connected
        backend.connect()
        assert backend.is_connected
        backend.disconnect()
        assert not backend.is_connected

    def test_stores_parsed_connection(self):
        backend = NwcWalletBackend(VALID_URI)
        assert backend._conn.wallet_pubkey == TEST_WALLET_PUBKEY
        assert backend._conn.client_secret == TEST_SECRET
        assert len(backend._client_pubkey) == 64


# ── NWC Request/Response ─────────────────────────────────────────────


class TestNwcRequests:
    """Tests for NWC request building and response parsing."""

    def test_build_event(self):
        backend = NwcWalletBackend(VALID_URI)
        event = backend._build_event(
            kind=23194,
            content="encrypted-content",
            tags=[["p", TEST_WALLET_PUBKEY]],
        )
        assert event["kind"] == 23194
        assert event["pubkey"] == backend._client_pubkey
        assert event["content"] == "encrypted-content"
        assert event["tags"] == [["p", TEST_WALLET_PUBKEY]]
        assert isinstance(event["created_at"], int)

    def test_compute_event_id(self):
        backend = NwcWalletBackend(VALID_URI)
        event = backend._build_event(
            kind=23194,
            content="test",
            tags=[],
        )
        event_id = backend._compute_event_id(event)
        assert len(event_id) == 64
        # Should be deterministic
        assert backend._compute_event_id(event) == event_id

    def test_sign_event(self):
        backend = NwcWalletBackend(VALID_URI)
        event_id = hashlib.sha256(b"test event").hexdigest()
        sig = backend._sign_event(event_id)
        assert len(sig) == 128  # 64 bytes hex

    @pytest.mark.asyncio
    async def test_get_balance_calls_nwc(self):
        """get_balance should send a get_balance NWC request."""
        backend = NwcWalletBackend(VALID_URI)
        backend.connect()

        mock_result = {"balance": 50000000}  # 50,000 sats in msats

        with patch.object(backend, "_send_nwc_request", return_value=mock_result):
            balance = backend.get_balance()
            assert balance["channel_balance_sats"] == 50000

    @pytest.mark.asyncio
    async def test_create_invoice_calls_nwc(self):
        """create_invoice should send a make_invoice NWC request."""
        backend = NwcWalletBackend(VALID_URI)
        backend.connect()

        mock_result = {
            "invoice": "lnbc500n1...",
            "payment_hash": "abc123",
        }

        with patch.object(backend, "_send_nwc_request", return_value=mock_result):
            invoice = backend.create_invoice(amount_msats=50000, memo="test")
            assert isinstance(invoice, InvoiceResponse)
            assert invoice.payment_request == "lnbc500n1..."
            assert invoice.payment_hash == "abc123"

    @pytest.mark.asyncio
    async def test_pay_invoice_success(self):
        """pay_invoice should return SUCCEEDED with preimage on success."""
        backend = NwcWalletBackend(VALID_URI)
        backend.connect()

        mock_result = {
            "preimage": "deadbeef" * 8,
            "fees_paid": 100,
        }

        with patch.object(backend, "_send_nwc_request", return_value=mock_result):
            result = backend.pay_invoice(payment_request="lnbc500n1...")
            assert isinstance(result, PaymentResponse)
            assert result.status == "SUCCEEDED"
            assert result.preimage == "deadbeef" * 8
            assert result.fee_msats == 100

    @pytest.mark.asyncio
    async def test_pay_invoice_failure(self):
        """pay_invoice should return FAILED on NWC error."""
        backend = NwcWalletBackend(VALID_URI)
        backend.connect()

        with patch.object(
            backend, "_send_nwc_request",
            side_effect=NwcError("PAYMENT_FAILED: no route"),
        ):
            result = backend.pay_invoice(payment_request="lnbc500n1...")
            assert result.status == "FAILED"
            assert "PAYMENT_FAILED" in result.failure_reason

    @pytest.mark.asyncio
    async def test_lookup_invoice_settled(self):
        """lookup_invoice should return settled=True for settled invoices."""
        backend = NwcWalletBackend(VALID_URI)
        backend.connect()

        mock_result = {
            "state": "settled",
            "invoice": "lnbc500n1...",
            "amount": 50000,
            "preimage": "abc123",
            "description": "test payment",
        }

        with patch.object(backend, "_send_nwc_request", return_value=mock_result):
            result = backend.lookup_invoice(payment_hash="hash123")
            assert result["settled"] is True
            assert result["preimage"] == "abc123"

    @pytest.mark.asyncio
    async def test_lookup_invoice_pending(self):
        """lookup_invoice should return settled=False for pending invoices."""
        backend = NwcWalletBackend(VALID_URI)
        backend.connect()

        mock_result = {
            "state": "pending",
            "amount": 50000,
        }

        with patch.object(backend, "_send_nwc_request", return_value=mock_result):
            result = backend.lookup_invoice(payment_hash="hash123")
            assert result["settled"] is False

    @pytest.mark.asyncio
    async def test_get_info(self):
        """get_info should return NodeInfo with backend_type='nwc'."""
        backend = NwcWalletBackend(VALID_URI)
        backend.connect()

        mock_result = {
            "alias": "My Alby Wallet",
            "pubkey": "deadbeef" * 8,
            "block_height": 850000,
        }

        with patch.object(backend, "_send_nwc_request", return_value=mock_result):
            info = backend.get_info()
            assert isinstance(info, NodeInfo)
            assert info.backend_type == "nwc"
            assert info.alias == "My Alby Wallet"


# ── Edge Cases ───────────────────────────────────────────────────────


class TestNwcEdgeCases:
    """Edge cases and error handling."""

    def test_sign_and_verify_message(self):
        """sign_message and verify_message should round-trip."""
        backend = NwcWalletBackend(VALID_URI)
        message = "test challenge message"
        sig = backend.sign_message(message)
        result = backend.verify_message(message, sig)
        assert result["valid"] is True

    def test_verify_bad_signature(self):
        """verify_message should reject invalid signatures."""
        backend = NwcWalletBackend(VALID_URI)
        result = backend.verify_message("test", "00" * 64)
        assert result["valid"] is False

    @pytest.mark.asyncio
    async def test_lookup_invoice_nwc_error(self):
        """lookup_invoice should return safe defaults on NWC error."""
        backend = NwcWalletBackend(VALID_URI)
        backend.connect()

        with patch.object(
            backend, "_send_nwc_request",
            side_effect=NwcError("NOT_FOUND"),
        ):
            result = backend.lookup_invoice(payment_hash="nonexistent")
            assert result["settled"] is False
            assert result["state"] == "unknown"


# ── NIP-44 v2 Encryption ─────────────────────────────────────────────

# Official NIP-44 v2 vectors, vendored from github.com/paulmillr/nip44
# (nip44.vectors.json). These are the canonical cross-implementation
# conformance vectors referenced by NIP-44.
_NIP44 = json.loads(
    (Path(__file__).parent / "vectors" / "nip44.vectors.json").read_text()
)["v2"]["valid"]


def _backend_with_secret(secret_hex: str) -> NwcWalletBackend:
    """Build a backend whose client secret is `secret_hex`.

    The wallet pubkey in the URI is irrelevant for these crypto tests — every
    method under test takes the counterparty pubkey as an explicit argument.
    """
    return NwcWalletBackend(
        f"nostr+walletconnect://{'00' * 32}?relay={TEST_RELAY}&secret={secret_hex}"
    )


class TestNip44Vectors:
    """Byte-exact conformance against the official NIP-44 v2 test vectors."""

    @pytest.mark.parametrize("v", _NIP44["get_conversation_key"])
    def test_conversation_key(self, v):
        """ECDH → HKDF-extract(salt='nip44-v2') must match spec, byte for byte."""
        backend = _backend_with_secret(v["sec1"])
        assert backend._get_conversation_key(v["pub2"]).hex() == v["conversation_key"]

    @pytest.mark.parametrize("k", _NIP44["get_message_keys"]["keys"])
    def test_message_keys(self, k):
        """HKDF-expand(conversation_key, nonce, 76) → chacha key/nonce + hmac key."""
        ck = bytes.fromhex(_NIP44["get_message_keys"]["conversation_key"])
        chacha_key, chacha_nonce, hmac_key = _backend_with_secret(
            "11" * 32
        )._get_message_keys(ck, bytes.fromhex(k["nonce"]))
        assert chacha_key.hex() == k["chacha_key"]
        assert chacha_nonce.hex() == k["chacha_nonce"]
        assert hmac_key.hex() == k["hmac_key"]

    @pytest.mark.parametrize(
        "unpadded,padded",
        [(u, p) for u, p in _NIP44["calc_padded_len"] if 1 <= u <= 65535],
    )
    def test_padding_length(self, unpadded, padded):
        """_nip44_pad prepends a 2-byte length prefix to the padded buffer."""
        assert len(NwcWalletBackend._nip44_pad("a" * unpadded)) - 2 == padded

    def test_padding_rejects_oversize(self):
        """Plaintext above the 65535-byte encryptable max must be rejected."""
        with pytest.raises(NwcError, match="out of range"):
            NwcWalletBackend._nip44_pad("a" * 65536)

    @pytest.mark.parametrize("v", _NIP44["encrypt_decrypt"])
    def test_decrypt_payload(self, v):
        """Decrypt a spec-produced payload back to its plaintext."""
        pub2 = _derive_pubkey_from_secret(v["sec2"])
        assert _backend_with_secret(v["sec1"])._nip44_decrypt(v["payload"], pub2) == v[
            "plaintext"
        ]

    @pytest.mark.parametrize("v", _NIP44["encrypt_decrypt"])
    def test_encrypt_payload_byte_exact(self, v):
        """With the vector's nonce injected, encryption reproduces the exact payload."""
        pub2 = _derive_pubkey_from_secret(v["sec2"])
        with patch("os.urandom", return_value=bytes.fromhex(v["nonce"])):
            out = _backend_with_secret(v["sec1"])._nip44_encrypt(v["plaintext"], pub2)
        assert out == v["payload"]


class TestNip44Encryption:
    """Round-trip, tamper detection, and NIP-04/NIP-44 auto-detection."""

    @staticmethod
    def _two_parties():
        alice_sec, bob_sec = secrets.token_hex(32), secrets.token_hex(32)
        alice = (_backend_with_secret(alice_sec), _derive_pubkey_from_secret(alice_sec))
        bob = (_backend_with_secret(bob_sec), _derive_pubkey_from_secret(bob_sec))
        return alice, bob

    @pytest.mark.parametrize(
        "msg",
        [
            "a",
            "balance",
            "x" * 31,
            "y" * 32,  # padding boundary
            "z" * 33,  # padding boundary
            "u" * 1000,
            '{"method": "get_balance", "params": {}}',
            "emoji 🛰️ payload",  # multibyte UTF-8
        ],
    )
    def test_round_trip(self, msg):
        (alice, alice_pub), (bob, bob_pub) = self._two_parties()
        ciphertext = alice._nip44_encrypt(msg, bob_pub)
        assert bob._nip44_decrypt(ciphertext, alice_pub) == msg

    def test_conversation_key_symmetric(self):
        """ck(alice_priv, bob_pub) == ck(bob_priv, alice_pub)."""
        (alice, alice_pub), (bob, bob_pub) = self._two_parties()
        assert alice._get_conversation_key(bob_pub) == bob._get_conversation_key(
            alice_pub
        )

    def test_random_nonce_differs(self):
        """Two encryptions of the same plaintext differ (random 32-byte nonce)."""
        (alice, _), (_, bob_pub) = self._two_parties()
        assert alice._nip44_encrypt("hello", bob_pub) != alice._nip44_encrypt(
            "hello", bob_pub
        )

    def test_version_byte(self):
        (alice, _), (_, bob_pub) = self._two_parties()
        assert base64.b64decode(alice._nip44_encrypt("hi", bob_pub))[0] == 0x02

    def test_tamper_fails_mac(self):
        """Flipping a ciphertext byte must fail HMAC verification."""
        (alice, alice_pub), (bob, bob_pub) = self._two_parties()
        raw = bytearray(base64.b64decode(alice._nip44_encrypt("secret", bob_pub)))
        raw[40] ^= 0x01  # somewhere inside the ciphertext
        tampered = base64.b64encode(bytes(raw)).decode()
        with pytest.raises(NwcError, match="MAC"):
            bob._nip44_decrypt(tampered, alice_pub)

    def test_decrypt_rejects_unknown_version(self):
        (alice, alice_pub), (bob, bob_pub) = self._two_parties()
        raw = bytearray(base64.b64decode(alice._nip44_encrypt("hi", bob_pub)))
        raw[0] = 0x01  # unsupported version
        with pytest.raises(NwcError, match="version"):
            bob._nip44_decrypt(base64.b64encode(bytes(raw)).decode(), alice_pub)

    def test_decrypt_rejects_short_payload(self):
        bob = _backend_with_secret(secrets.token_hex(32))
        short = base64.b64encode(b"\x02" + b"\x00" * 10).decode()
        with pytest.raises(NwcError, match="too short"):
            bob._nip44_decrypt(short, "00" * 32)

    def test_auto_detect_routes_nip44(self):
        """_decrypt should route a NIP-44 blob (no ?iv= marker) to NIP-44."""
        (alice, alice_pub), (bob, bob_pub) = self._two_parties()
        ciphertext = alice._nip44_encrypt("hello", bob_pub)
        assert bob._decrypt(ciphertext, alice_pub) == "hello"

    def test_auto_detect_routes_nip04(self):
        """_decrypt should route a NIP-04 blob (has ?iv= marker) to NIP-04."""
        (alice, alice_pub), (bob, bob_pub) = self._two_parties()
        ciphertext = alice._nip04_encrypt("hello", bob_pub)
        assert bob._decrypt(ciphertext, alice_pub) == "hello"
