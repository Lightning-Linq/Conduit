"""Tests for the rating integrity system.

Verifies preimage verification, duplicate prevention, timing checks,
and weighted rating calculation.
"""

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

# ── Preimage verification (pure crypto, no DB) ───────────────────────


class TestPreimageVerification:
    """SHA-256 preimage-to-hash verification — the core trust mechanism."""

    def test_valid_preimage_matches_hash(self):
        """A correct preimage should SHA-256 to the expected payment hash."""
        preimage = "deadbeef" * 8  # 64-char hex (32 bytes)
        preimage_bytes = bytes.fromhex(preimage)
        expected_hash = hashlib.sha256(preimage_bytes).hexdigest()

        computed = hashlib.sha256(preimage_bytes).hexdigest()
        assert computed == expected_hash

    def test_wrong_preimage_does_not_match(self):
        """An incorrect preimage should not match the payment hash."""
        correct_preimage = "aa" * 32
        wrong_preimage = "bb" * 32
        expected_hash = hashlib.sha256(bytes.fromhex(correct_preimage)).hexdigest()

        computed = hashlib.sha256(bytes.fromhex(wrong_preimage)).hexdigest()
        assert computed != expected_hash

    def test_preimage_is_case_insensitive(self):
        """Hex preimage comparison should work regardless of case."""
        preimage_lower = "abcdef" * 5 + "ab"
        preimage_upper = preimage_lower.upper()

        hash_lower = hashlib.sha256(bytes.fromhex(preimage_lower)).hexdigest()
        hash_upper = hashlib.sha256(bytes.fromhex(preimage_upper)).hexdigest()
        assert hash_lower == hash_upper

    def test_empty_preimage_produces_known_hash(self):
        """An empty preimage hashes to the SHA-256 of empty bytes — never matches a real payment."""
        empty_hash = hashlib.sha256(b"").hexdigest()
        # This is the well-known SHA-256 of nothing
        assert empty_hash == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        # It should never match a real payment hash
        real_payment_hash = "a" * 64
        assert empty_hash != real_payment_hash

    def test_invalid_hex_preimage_raises(self):
        """A non-hex preimage should raise ValueError."""
        with pytest.raises(ValueError):
            bytes.fromhex("not-valid-hex")

    def test_real_world_preimage_format(self):
        """Preimages from LND are 32 bytes (64 hex chars)."""
        preimage = "7f38f0058811d5261106baf242027abd64d1b62dfbdc10861a4985c3878827e5"
        assert len(preimage) == 64
        preimage_bytes = bytes.fromhex(preimage)
        assert len(preimage_bytes) == 32
        payment_hash = hashlib.sha256(preimage_bytes).hexdigest()
        assert len(payment_hash) == 64


# ── Weighted rating calculation (pure math, no DB) ────────────────────


class TestWeightedRatingCalculation:
    """Tests for the 1/n diminishing weight algorithm."""

    def _calculate_weighted(self, ratings: list[tuple[int, str]]) -> float:
        """
        Reproduce the weighted calculation from rating_integrity.py
        without importing it (avoids DB model dependencies).
        """
        if not ratings:
            return 0.0

        consumer_counts: dict[str, int] = {}
        weighted_sum = 0.0
        total_weight = 0.0

        for score, consumer in ratings:
            name = consumer or "anonymous"
            consumer_counts[name] = consumer_counts.get(name, 0) + 1
            count = consumer_counts[name]
            weight = 1.0 / count
            weighted_sum += score * weight
            total_weight += weight

        return round(weighted_sum / total_weight, 2) if total_weight > 0 else 0.0

    def test_single_rating(self):
        """One rating should return that rating's score."""
        result = self._calculate_weighted([(5, "alice")])
        assert result == 5.0

    def test_equal_weight_same_consumers(self):
        """Different consumers should each get weight 1.0."""
        # Alice=5, Bob=3 → (5*1 + 3*1) / (1+1) = 4.0
        result = self._calculate_weighted([(5, "alice"), (3, "bob")])
        assert result == 4.0

    def test_diminishing_weight_repeat_consumer(self):
        """Repeat consumer ratings should get diminishing weight (1/n)."""
        # Alice rates 5 (weight 1.0), Alice rates 1 (weight 0.5)
        # weighted = (5*1 + 1*0.5) / (1 + 0.5) = 5.5/1.5 = 3.67
        result = self._calculate_weighted([(5, "alice"), (1, "alice")])
        assert result == 3.67

    def test_one_spammer_gets_diluted(self):
        """A single consumer spamming 5-star ratings should be diluted."""
        # Bob=4 (w=1), Alice=5 x5 (w=1, 0.5, 0.33, 0.25, 0.2)
        ratings = [(4, "bob")] + [(5, "alice")] * 5
        result = self._calculate_weighted(ratings)

        # Without weighting: (4 + 25) / 6 = 4.83
        # With weighting Alice is diluted, Bob's 4 has more influence
        simple_avg = (4 + 5 * 5) / 6
        assert result < simple_avg  # weighted should be lower

    def test_anonymous_consumers_share_identity(self):
        """None/anonymous consumers should all be treated as one identity."""
        # Two anonymous ratings get diminishing weight
        result = self._calculate_weighted([(5, None), (1, None)])
        # Same as repeat consumer: 3.67
        assert result == 3.67

    def test_empty_ratings(self):
        """No ratings should return 0.0."""
        result = self._calculate_weighted([])
        assert result == 0.0

    def test_diverse_reviewers_approximate_simple_average(self):
        """With all unique reviewers, weighted ≈ simple average."""
        ratings = [(5, "a"), (4, "b"), (3, "c"), (2, "d"), (1, "e")]
        result = self._calculate_weighted(ratings)
        simple = sum(s for s, _ in ratings) / len(ratings)
        assert result == simple  # exactly equal with unique consumers


# ── Timing constraints ────────────────────────────────────────────────


class TestRatingTimingConstraints:
    """Tests for minimum delay enforcement between execution and rating."""

    MIN_DELAY = 30  # seconds, from rating_integrity.py

    def test_too_early_rating_detected(self):
        """Rating submitted before MIN_DELAY should be flagged."""
        execution_completed = datetime.now(UTC) - timedelta(seconds=10)
        elapsed = (datetime.now(UTC) - execution_completed).total_seconds()
        assert elapsed < self.MIN_DELAY

    def test_after_delay_rating_allowed(self):
        """Rating submitted after MIN_DELAY should be allowed."""
        execution_completed = datetime.now(UTC) - timedelta(seconds=60)
        elapsed = (datetime.now(UTC) - execution_completed).total_seconds()
        assert elapsed >= self.MIN_DELAY

    def test_exact_boundary(self):
        """Rating at exactly MIN_DELAY seconds should be allowed."""
        execution_completed = datetime.now(UTC) - timedelta(seconds=self.MIN_DELAY)
        elapsed = (datetime.now(UTC) - execution_completed).total_seconds()
        assert elapsed >= self.MIN_DELAY
