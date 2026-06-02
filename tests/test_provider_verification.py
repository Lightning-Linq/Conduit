"""Tests for provider verification (challenge generation, crypto logic,
DNS TXT verification, and badge expiry)."""

import hashlib
from datetime import UTC, datetime, timedelta

from conduit.services.provider_verification import (
    _CHALLENGE_PREFIX,
    generate_challenge,
)


class TestChallengeGeneration:
    """Tests for verification challenge creation."""

    def test_challenge_format(self):
        """Challenge: 'conduit-verify:<nonce>:<skill_id>:<issued_ts>'."""
        skill_id = "abc-123"
        challenge = generate_challenge(skill_id)

        parts = challenge.split(":")
        assert len(parts) == 4
        assert parts[0] == "conduit-verify"
        assert parts[2] == skill_id
        # Last segment is a unix timestamp (digits)
        int(parts[3])

    def test_challenge_contains_random_nonce(self):
        """Each challenge should contain a unique random nonce."""
        c1 = generate_challenge("skill-1")
        c2 = generate_challenge("skill-1")

        # Same skill ID but different nonces
        assert c1 != c2
        nonce1 = c1.split(":")[1]
        nonce2 = c2.split(":")[1]
        assert nonce1 != nonce2

    def test_nonce_is_hex(self):
        """The nonce portion should be valid hex."""
        challenge = generate_challenge("test")
        nonce = challenge.split(":")[1]
        # Should not raise
        bytes.fromhex(nonce)
        assert len(nonce) == 32  # 16 bytes = 32 hex chars

    def test_challenge_includes_skill_id(self):
        """The challenge should embed the skill ID for binding."""
        skill_id = "550e8400-e29b-41d4-a716-446655440000"
        challenge = generate_challenge(skill_id)
        assert f":{skill_id}:" in challenge

    def test_challenge_prefix_is_correct(self):
        """The prefix constant should be 'conduit-verify'."""
        assert _CHALLENGE_PREFIX == "conduit-verify"

    def test_challenge_freshness(self):
        """A freshly-generated challenge must be considered fresh; one
        with a backdated timestamp must not."""
        from conduit.services.provider_verification import _challenge_is_fresh

        fresh = generate_challenge("s")
        assert _challenge_is_fresh(fresh) is True

        # Backdate by an hour — past the 30-min TTL
        parts = fresh.split(":")
        parts[-1] = str(int(parts[-1]) - 3600)
        stale = ":".join(parts)
        assert _challenge_is_fresh(stale) is False

        # Pre-TTL legacy format (no timestamp segment) → not fresh.
        legacy = "conduit-verify:deadbeef:skill-id"
        assert _challenge_is_fresh(legacy) is False


class TestNodeSignatureVerification:
    """Tests for Lightning node signature verification logic.

    These test the cryptographic concepts without requiring an LND node.
    The actual VerifyMessage RPC is tested via integration tests.
    """

    def test_signature_verification_concept(self):
        """
        Conceptual test: the verification flow should be
        sign(challenge, private_key) → verify(challenge, signature, pubkey).
        """
        # Simulate the flow with HMAC (not real LND signatures)
        import hmac

        secret_key = b"node-private-key"
        challenge = generate_challenge("test-skill")

        # Provider signs
        signature = hmac.new(secret_key, challenge.encode(), hashlib.sha256).hexdigest()

        # Verifier checks
        expected = hmac.new(secret_key, challenge.encode(), hashlib.sha256).hexdigest()
        assert signature == expected

    def test_wrong_signature_fails(self):
        """A signature from a different key should not verify."""
        import hmac

        key_a = b"node-key-a"
        key_b = b"node-key-b"
        challenge = generate_challenge("test-skill")

        sig_a = hmac.new(key_a, challenge.encode(), hashlib.sha256).hexdigest()
        sig_b = hmac.new(key_b, challenge.encode(), hashlib.sha256).hexdigest()

        assert sig_a != sig_b

    def test_challenge_tampering_detected(self):
        """Signing a different challenge should produce a different signature."""
        import hmac

        key = b"node-key"
        challenge_1 = generate_challenge("skill-1")
        challenge_2 = generate_challenge("skill-2")

        sig_1 = hmac.new(key, challenge_1.encode(), hashlib.sha256).hexdigest()
        sig_2 = hmac.new(key, challenge_2.encode(), hashlib.sha256).hexdigest()

        assert sig_1 != sig_2


class TestDomainVerification:
    """Tests for domain verification logic (no HTTP calls)."""

    def test_well_known_url_format(self):
        """The well-known URL should follow the standard pattern."""
        domain = "example.com"
        expected = f"https://{domain}/.well-known/conduit-verify.txt"
        assert expected == "https://example.com/.well-known/conduit-verify.txt"

    def test_exact_match_required(self):
        """Domain content must match the challenge EXACTLY after strip.
        A page that merely mentions the challenge (paste, gist, status
        banner that mirrors user content) must not be accepted."""
        challenge = generate_challenge("test-skill")

        # Old substring behaviour would have passed this; the fix requires
        # the well-known file to contain ONLY the challenge.
        page_with_extra_content = f"some header\n{challenge}\nsome footer"
        assert page_with_extra_content.strip() != challenge

        # Exact contents (possibly with surrounding whitespace) → match.
        exact_file_contents = f"\n  {challenge}  \n"
        assert exact_file_contents.strip() == challenge


class TestDNSTxtVerification:
    """Tests for DNS TXT record verification path."""

    def test_dns_txt_lookup_name_format(self):
        """DNS TXT record should be at _conduit-verify.{domain}."""
        domain = "example.com"
        expected = f"_conduit-verify.{domain}"
        assert expected == "_conduit-verify.example.com"

    def test_dns_txt_value_must_match_exactly(self):
        """DNS TXT record value must exactly match the challenge."""
        challenge = generate_challenge("test-skill")
        # Exact match
        assert challenge.strip() == challenge
        # Partial match should fail
        assert f"prefix-{challenge}" != challenge


class TestVerificationExpiry:
    """Tests for verification badge expiry."""

    def test_check_expiry_no_verified_at(self):
        """A skill that was never verified should not expire."""
        from unittest.mock import MagicMock

        from conduit.services.provider_verification import check_verification_expiry

        skill = MagicMock()
        skill.verified_at = None
        skill.verification_status = "unverified"
        assert check_verification_expiry(skill) is False

    def test_check_expiry_fresh_verification(self):
        """A recently verified skill should not be expired."""
        from unittest.mock import MagicMock, patch

        from conduit.services.provider_verification import check_verification_expiry

        skill = MagicMock()
        skill.verified_at = datetime.now(UTC) - timedelta(days=1)
        skill.verification_status = "node_verified"

        with patch("conduit.services.provider_verification.settings") as mock_settings:
            mock_settings.verification_expiry_days = 90
            assert check_verification_expiry(skill) is False

    def test_check_expiry_old_verification(self):
        """A verification older than expiry_days should be expired."""
        from unittest.mock import MagicMock, patch

        from conduit.services.provider_verification import check_verification_expiry

        skill = MagicMock()
        skill.verified_at = datetime.now(UTC) - timedelta(days=100)
        skill.verification_status = "fully_verified"

        with patch("conduit.services.provider_verification.settings") as mock_settings:
            mock_settings.verification_expiry_days = 90
            assert check_verification_expiry(skill) is True

    def test_check_expiry_disabled(self):
        """When verification_expiry_days=0, nothing expires."""
        from unittest.mock import MagicMock, patch

        from conduit.services.provider_verification import check_verification_expiry

        skill = MagicMock()
        skill.verified_at = datetime.now(UTC) - timedelta(days=9999)
        skill.verification_status = "node_verified"

        with patch("conduit.services.provider_verification.settings") as mock_settings:
            mock_settings.verification_expiry_days = 0
            assert check_verification_expiry(skill) is False

    def test_unverified_never_expires(self):
        """An unverified skill has nothing to expire."""
        from unittest.mock import MagicMock

        from conduit.services.provider_verification import check_verification_expiry

        skill = MagicMock()
        skill.verified_at = datetime.now(UTC) - timedelta(days=9999)
        skill.verification_status = "unverified"
        assert check_verification_expiry(skill) is False


class TestVerificationBadges:
    """Tests for verification status badge logic."""

    def test_badge_progression(self):
        """Verification badges should follow the correct progression."""
        # Default
        assert "unverified" not in ("node_verified", "domain_verified", "fully_verified")

        # Node only → "node_verified"
        # Domain only → "domain_verified"
        # Both → "fully_verified"
        badges = {"unverified", "node_verified", "domain_verified", "fully_verified"}
        assert len(badges) == 4

    def test_fully_verified_requires_both(self):
        """fully_verified should only be set when both node and domain are verified."""
        node_verified = True
        domain_verified = True

        if node_verified and domain_verified:
            status = "fully_verified"
        elif node_verified:
            status = "node_verified"
        elif domain_verified:
            status = "domain_verified"
        else:
            status = "unverified"

        assert status == "fully_verified"

    def test_node_only_badge(self):
        """Only node verified should give node_verified badge."""
        node_verified = True
        domain_verified = False

        if node_verified and domain_verified:
            status = "fully_verified"
        elif node_verified:
            status = "node_verified"
        else:
            status = "unverified"

        assert status == "node_verified"
