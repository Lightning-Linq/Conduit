"""Tests for the L402 protocol — macaroon minting, stateless verification,
header parsing, middleware integration, and security properties."""

import hashlib
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch

import pytest
from pymacaroons import Macaroon

from conduit.services.l402 import (
    L402Challenge,
    L402Credential,
    L402VerifyResult,
    mint_l402_macaroon,
    create_l402_challenge,
    parse_l402_header,
    verify_l402,
    format_www_authenticate,
    _get_l402_secret,
    _LOCATION,
)


# ── Helpers ──────────────────────────────────────────────────────────

def _make_payment_pair() -> tuple[str, str]:
    """Generate a random preimage/payment_hash pair."""
    import os
    preimage = os.urandom(32)
    preimage_hex = preimage.hex()
    payment_hash = hashlib.sha256(preimage).hexdigest()
    return preimage_hex, payment_hash


def _make_valid_credential(
    *,
    resource: str | None = None,
    expires_at: datetime | None = None,
) -> tuple[L402Credential, str, str]:
    """
    Mint a macaroon with a matching preimage and return
    (credential, preimage_hex, payment_hash).
    """
    preimage_hex, payment_hash = _make_payment_pair()
    macaroon = mint_l402_macaroon(
        payment_hash,
        resource=resource,
        expires_at=expires_at,
    )
    cred = L402Credential(macaroon_raw=macaroon, preimage=preimage_hex)
    return cred, preimage_hex, payment_hash


# ── Secret derivation ────────────────────────────────────────────────


class TestSecretDerivation:
    """L402 secret must be separate from the permission macaroon secret."""

    def test_l402_secret_is_deterministic(self):
        """Same API key should produce the same secret."""
        assert _get_l402_secret() == _get_l402_secret()

    def test_l402_secret_differs_from_permission_secret(self):
        """L402 tokens must not be verifiable as permission tokens."""
        from conduit.services.macaroon_auth import _get_secret as perm_secret
        assert _get_l402_secret() != perm_secret()


# ── Macaroon minting ────────────────────────────────────────────────


class TestMintL402Macaroon:
    """Minting L402 macaroons bound to a payment hash."""

    def test_basic_mint(self):
        """Minted macaroon should be a valid base64 string."""
        _, payment_hash = _make_payment_pair()
        mac = mint_l402_macaroon(payment_hash)
        # Should deserialize without error
        m = Macaroon.deserialize(mac)
        assert m.location == _LOCATION

    def test_payment_hash_caveat_present(self):
        """Minted macaroon must contain a payment_hash caveat."""
        _, payment_hash = _make_payment_pair()
        mac = mint_l402_macaroon(payment_hash)
        m = Macaroon.deserialize(mac)
        caveats = [c.caveat_id for c in m.caveats]
        assert any(f"payment_hash = {payment_hash}" in c for c in caveats)

    def test_resource_caveat(self):
        """When resource is specified, caveat should be added."""
        _, payment_hash = _make_payment_pair()
        mac = mint_l402_macaroon(payment_hash, resource="marketplace")
        m = Macaroon.deserialize(mac)
        caveats = [c.caveat_id for c in m.caveats]
        assert any("resource = marketplace" in c for c in caveats)

    def test_no_resource_caveat_when_none(self):
        """When no resource is specified, no resource caveat should appear."""
        _, payment_hash = _make_payment_pair()
        mac = mint_l402_macaroon(payment_hash)
        m = Macaroon.deserialize(mac)
        caveats = [c.caveat_id for c in m.caveats]
        assert not any("resource = " in c for c in caveats)

    def test_expires_caveat(self):
        """When expires_at is specified, caveat should be added."""
        _, payment_hash = _make_payment_pair()
        exp = datetime.now(timezone.utc) + timedelta(hours=1)
        mac = mint_l402_macaroon(payment_hash, expires_at=exp)
        m = Macaroon.deserialize(mac)
        caveats = [c.caveat_id for c in m.caveats]
        assert any(c.startswith("expires = ") for c in caveats)

    def test_identifier_contains_hash_prefix(self):
        """Macaroon identifier should contain the first 16 chars of payment_hash."""
        _, payment_hash = _make_payment_pair()
        mac = mint_l402_macaroon(payment_hash)
        m = Macaroon.deserialize(mac)
        assert payment_hash[:16] in m.identifier


# ── Stateless verification ──────────────────────────────────────────


class TestVerifyL402:
    """Core stateless verification: preimage proof, expiry, resource extraction."""

    def test_valid_credential_passes(self):
        """A correctly paired macaroon+preimage should verify."""
        cred, _, payment_hash = _make_valid_credential()
        result = verify_l402(cred)
        assert result.valid is True
        assert result.payment_hash == payment_hash
        assert result.error is None

    def test_wrong_preimage_fails(self):
        """Presenting the wrong preimage must be rejected."""
        _, payment_hash = _make_payment_pair()
        mac = mint_l402_macaroon(payment_hash)

        # Generate a different preimage
        wrong_preimage = "aa" * 32
        cred = L402Credential(macaroon_raw=mac, preimage=wrong_preimage)
        result = verify_l402(cred)
        assert result.valid is False
        assert "preimage" in result.error.lower() or "payment" in result.error.lower()

    def test_tampered_macaroon_fails(self):
        """A macaroon with a modified signature should fail verification."""
        cred, _, _ = _make_valid_credential()
        # Corrupt the macaroon by replacing a character
        corrupted = cred.macaroon_raw[:-2] + "XX"
        cred_bad = L402Credential(macaroon_raw=corrupted, preimage=cred.preimage)
        result = verify_l402(cred_bad)
        assert result.valid is False

    def test_garbage_macaroon_fails(self):
        """Total garbage in the macaroon field should fail gracefully."""
        cred = L402Credential(macaroon_raw="not-a-macaroon", preimage="aa" * 32)
        result = verify_l402(cred)
        assert result.valid is False
        assert "invalid macaroon" in result.error.lower()

    def test_resource_extracted(self):
        """Resource scope should be returned when present in macaroon."""
        cred, _, _ = _make_valid_credential(resource="lightning")
        result = verify_l402(cred)
        assert result.valid is True
        assert result.resource == "lightning"

    def test_no_resource_returns_none(self):
        """When no resource caveat exists, resource should be None."""
        cred, _, _ = _make_valid_credential()
        result = verify_l402(cred)
        assert result.valid is True
        assert result.resource is None

    def test_expired_token_rejected(self):
        """A token with an expired timestamp must be rejected."""
        # Expired 1 hour ago
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        cred, _, _ = _make_valid_credential(expires_at=past)
        result = verify_l402(cred)
        assert result.valid is False
        assert "expired" in result.error.lower()

    def test_future_expiry_accepted(self):
        """A token expiring in the future should be accepted."""
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        cred, _, _ = _make_valid_credential(expires_at=future)
        result = verify_l402(cred)
        assert result.valid is True

    def test_unrecognized_caveat_rejected(self):
        """A macaroon with an unknown caveat type must fail — fail closed."""
        preimage_hex, payment_hash = _make_payment_pair()
        mac = mint_l402_macaroon(payment_hash)
        m = Macaroon.deserialize(mac)
        m.add_first_party_caveat("custom_caveat = something_unknown")
        tampered = m.serialize()

        cred = L402Credential(macaroon_raw=tampered, preimage=preimage_hex)
        result = verify_l402(cred)
        assert result.valid is False
        assert "unrecognized" in result.error.lower()


# ── Cross-token confusion ───────────────────────────────────────────


class TestTokenIsolation:
    """L402 tokens must never be confused with permission macaroons."""

    def test_permission_macaroon_cannot_pass_l402_verification(self):
        """A permission macaroon from macaroon_auth should not verify as L402."""
        from conduit.services.macaroon_auth import mint_root_macaroon
        root = mint_root_macaroon()
        # Try presenting it as an L402 credential with a random preimage
        cred = L402Credential(macaroon_raw=root, preimage="bb" * 32)
        result = verify_l402(cred)
        # It should fail because the signature was computed with a different key
        assert result.valid is False

    def test_l402_macaroon_cannot_pass_permission_verification(self):
        """An L402 macaroon should not verify as a permission macaroon."""
        from conduit.services.macaroon_auth import verify_macaroon
        _, payment_hash = _make_payment_pair()
        mac = mint_l402_macaroon(payment_hash)
        with pytest.raises(ValueError):
            verify_macaroon(mac)


# ── Header parsing ──────────────────────────────────────────────────


class TestParseL402Header:
    """Parse Authorization: L402 <macaroon>:<preimage> headers."""

    def test_valid_header(self):
        """A well-formed L402 header should parse correctly."""
        mac_b64 = "MDAxYWxvY2F0aW9uIHRlc3QK"  # valid base64
        preimage = "ab" * 32
        header = f"L402 {mac_b64}:{preimage}"
        cred = parse_l402_header(header)
        assert cred is not None
        assert cred.macaroon_raw == mac_b64
        assert cred.preimage == preimage

    def test_empty_header_returns_none(self):
        """Empty string should return None (no L402 scheme)."""
        assert parse_l402_header("") is None

    def test_bearer_header_returns_none(self):
        """A Bearer token should not match L402."""
        assert parse_l402_header("Bearer some-token") is None

    def test_missing_preimage_returns_none(self):
        """L402 without preimage should not match."""
        assert parse_l402_header("L402 MDAxYWxvY2F0aW9uIHRlc3QK") is None

    def test_short_preimage_returns_none(self):
        """Preimage must be exactly 64 hex chars (32 bytes)."""
        assert parse_l402_header("L402 MDAxYWxvY2F0aW9uIHRlc3QK:aabb") is None

    def test_non_hex_preimage_returns_none(self):
        """Non-hex characters in preimage should fail."""
        bad_preimage = "zz" * 32
        assert parse_l402_header(f"L402 MDAxYWxvY2F0aW9uIHRlc3QK:{bad_preimage}") is None

    def test_whitespace_stripped(self):
        """Leading/trailing whitespace in header should be tolerated."""
        mac_b64 = "MDAxYWxvY2F0aW9uIHRlc3QK"
        preimage = "cd" * 32
        header = f"  L402 {mac_b64}:{preimage}  "
        cred = parse_l402_header(header)
        assert cred is not None


# ── WWW-Authenticate formatting ─────────────────────────────────────


class TestFormatWwwAuthenticate:
    """Format the WWW-Authenticate response header."""

    def test_format_contains_macaroon_and_invoice(self):
        """Output should include both macaroon and invoice."""
        challenge = L402Challenge(
            macaroon="mac123",
            invoice="lnbc1234",
            payment_hash="hash123",
            amount_sats=10,
            expires_at="2099-01-01T00:00:00+00:00",
        )
        header = format_www_authenticate(challenge)
        assert 'macaroon="mac123"' in header
        assert 'invoice="lnbc1234"' in header
        assert header.startswith("L402 ")


# ── Create L402 challenge (full flow) ───────────────────────────────


class TestCreateL402Challenge:
    """Integration test for creating a challenge with a mock LND client."""

    def test_challenge_flow(self):
        """create_l402_challenge should mint invoice + macaroon."""
        mock_lnd = MagicMock()
        mock_invoice = MagicMock()
        mock_invoice.payment_hash = "aa" * 32
        mock_invoice.payment_request = "lnbc10n1ptest..."
        mock_lnd.create_invoice.return_value = mock_invoice

        challenge = create_l402_challenge(
            mock_lnd,
            amount_sats=10,
            memo="test challenge",
            resource="marketplace",
        )

        assert isinstance(challenge, L402Challenge)
        assert challenge.payment_hash == "aa" * 32
        assert challenge.amount_sats == 10
        assert challenge.invoice == "lnbc10n1ptest..."
        # Macaroon should be non-empty and deserializable
        m = Macaroon.deserialize(challenge.macaroon)
        assert m.location == _LOCATION
        # Invoice creation should have been called with msats
        mock_lnd.create_invoice.assert_called_once()
        call_kwargs = mock_lnd.create_invoice.call_args
        assert call_kwargs.kwargs.get("amount_msats") == 10_000

    def test_challenge_has_expiry(self):
        """Challenge should have a non-null expires_at."""
        mock_lnd = MagicMock()
        mock_invoice = MagicMock()
        mock_invoice.payment_hash = "bb" * 32
        mock_invoice.payment_request = "lnbc20n1ptest..."
        mock_lnd.create_invoice.return_value = mock_invoice

        challenge = create_l402_challenge(mock_lnd, amount_sats=20)
        assert challenge.expires_at is not None


# ── End-to-end round-trip ───────────────────────────────────────────


class TestL402RoundTrip:
    """Full cycle: mint → format header → parse → verify."""

    def test_full_cycle(self):
        """Mint a token, format it as a header, parse it back, and verify."""
        preimage_hex, payment_hash = _make_payment_pair()
        resource = "marketplace"
        future = datetime.now(timezone.utc) + timedelta(hours=1)

        # Mint
        mac = mint_l402_macaroon(
            payment_hash, resource=resource, expires_at=future,
        )

        # Build header as a client would
        header = f"L402 {mac}:{preimage_hex}"

        # Parse
        cred = parse_l402_header(header)
        assert cred is not None

        # Verify
        result = verify_l402(cred)
        assert result.valid is True
        assert result.payment_hash == payment_hash
        assert result.resource == resource
        assert result.error is None


# ── Middleware unit tests ───────────────────────────────────────────


class TestL402Middleware:
    """Tests for the L402 middleware dispatch logic."""

    def test_free_routes_pass_through(self):
        """Routes in the free list should not require auth."""
        from conduit.api.middleware.l402 import L402Middleware
        mw = L402Middleware(app=None)
        assert mw._is_free_route("/health") is True
        assert mw._is_free_route("/docs") is True
        assert mw._is_free_route("/") is True

    def test_api_routes_not_free(self):
        """API routes should not be free."""
        from conduit.api.middleware.l402 import L402Middleware
        mw = L402Middleware(app=None)
        assert mw._is_free_route("/api/v1/lightning/balance") is False
        assert mw._is_free_route("/api/v1/marketplace/skills") is False

    def test_route_to_resource_mapping(self):
        """Routes should map to correct resource scopes."""
        from conduit.api.middleware.l402 import L402Middleware
        mw = L402Middleware(app=None)
        assert mw._route_to_resource("/api/v1/lightning/balance") == "lightning"
        assert mw._route_to_resource("/api/v1/marketplace/skills") == "marketplace"
        assert mw._route_to_resource("/api/v1/security/status") == "security"
        assert mw._route_to_resource("/api/v1/nostr/relay") == "nostr"
        assert mw._route_to_resource("/health") is None

    def test_price_for_route_returns_default(self):
        """Price should return the configured default."""
        from conduit.api.middleware.l402 import L402Middleware
        mw = L402Middleware(app=None)
        price = mw._get_price_for_route("/api/v1/marketplace/skills")
        assert isinstance(price, int)
        assert price > 0
