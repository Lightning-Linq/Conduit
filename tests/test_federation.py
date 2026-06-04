"""Tests for federated reputation attestations (Federation #1, phase 1).

Covers the trust model: payment-proof verification, signed round-trip,
cross-node verification, and rejection of tampering, forged signatures, and
false payment claims.
"""

import hashlib
import secrets

import pytest

from conduit.services.federation import (
    CONDUIT_RATING_KIND,
    build_rating_attestation,
    parse_and_verify_rating,
    verify_payment_proof,
)
from conduit.services.nostr import NostrEvent, NostrKeypair

SKILL_ID = "11111111-1111-1111-1111-111111111111"
PROVIDER = "02" + "ab" * 32


def _fake_payment() -> tuple[str, str]:
    """Return (payment_hash, preimage) for a fake but internally-valid payment."""
    preimage = secrets.token_bytes(32).hex()
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    return payment_hash, preimage


class TestPaymentProof:
    def test_valid(self):
        ph, pre = _fake_payment()
        assert verify_payment_proof(ph, pre) is True

    def test_wrong_preimage(self):
        ph, _ = _fake_payment()
        _, other = _fake_payment()
        assert verify_payment_proof(ph, other) is False

    def test_uppercase_hash_ok(self):
        ph, pre = _fake_payment()
        assert verify_payment_proof(ph.upper(), pre) is True

    def test_garbage(self):
        assert verify_payment_proof("nothex", "alsonothex") is False
        assert verify_payment_proof(None, None) is False


class TestBuildRating:
    def test_round_trip(self):
        kp = NostrKeypair.generate()
        ph, pre = _fake_payment()
        ev = build_rating_attestation(
            skill_id=SKILL_ID, provider_pubkey=PROVIDER,
            payment_hash=ph, payment_preimage=pre, score=5, keypair=kp,
        )
        assert ev.kind == CONDUIT_RATING_KIND
        assert ev.pubkey == kp.pubkey_hex

        att = parse_and_verify_rating(ev)
        assert att is not None
        assert att.skill_id == SKILL_ID
        assert att.score == 5
        assert att.rater_pubkey == kp.pubkey_hex
        assert att.payment_hash == ph
        assert att.dedupe_key == ph

    def test_rejects_bad_score(self):
        kp = NostrKeypair.generate()
        ph, pre = _fake_payment()
        for bad in (0, 6, -1):
            with pytest.raises(ValueError, match="score"):
                build_rating_attestation(
                    skill_id=SKILL_ID, provider_pubkey=PROVIDER,
                    payment_hash=ph, payment_preimage=pre, score=bad, keypair=kp,
                )

    def test_rejects_bad_preimage(self):
        kp = NostrKeypair.generate()
        ph, _ = _fake_payment()
        _, other = _fake_payment()
        with pytest.raises(ValueError, match="preimage"):
            build_rating_attestation(
                skill_id=SKILL_ID, provider_pubkey=PROVIDER,
                payment_hash=ph, payment_preimage=other, score=4, keypair=kp,
            )


class TestVerifyRating:
    def _valid_event(self, score: int = 5) -> NostrEvent:
        kp = NostrKeypair.generate()
        ph, pre = _fake_payment()
        return build_rating_attestation(
            skill_id=SKILL_ID, provider_pubkey=PROVIDER,
            payment_hash=ph, payment_preimage=pre, score=score, keypair=kp,
        )

    def test_cross_node_verify(self):
        # Built by one party, verified by anyone holding only the public event.
        assert parse_and_verify_rating(self._valid_event()) is not None

    def test_tampered_score_rejected(self):
        ev = self._valid_event(score=1)
        for t in ev.tags:
            if t[0] == "score":
                t[1] = "5"  # inflate after signing -> id/sig mismatch
        assert parse_and_verify_rating(ev) is None

    def test_forged_signature_rejected(self):
        ev = self._valid_event()
        ev.sig = "00" * 64
        assert parse_and_verify_rating(ev) is None

    def test_lying_about_payment_rejected(self):
        # A genuinely-signed event claiming a payment the preimage doesn't match.
        kp = NostrKeypair.generate()
        ph, _ = _fake_payment()
        _, wrong = _fake_payment()
        ev = NostrEvent(
            kind=CONDUIT_RATING_KIND,
            tags=[["skill", SKILL_ID], ["p", PROVIDER], ["payment_hash", ph],
                  ["preimage", wrong], ["score", "5"]],
            content="",
        )
        ev.sign(kp)
        assert ev.verify() is True                 # signature is genuine
        assert parse_and_verify_rating(ev) is None  # but the payment claim is a lie

    def test_wrong_kind_rejected(self):
        kp = NostrKeypair.generate()
        ph, pre = _fake_payment()
        ev = NostrEvent(
            kind=1,
            tags=[["skill", SKILL_ID], ["payment_hash", ph], ["preimage", pre], ["score", "5"]],
            content="",
        )
        ev.sign(kp)
        assert parse_and_verify_rating(ev) is None

    def test_missing_tags_rejected(self):
        kp = NostrKeypair.generate()
        ev = NostrEvent(kind=CONDUIT_RATING_KIND, tags=[["score", "5"]], content="")
        ev.sign(kp)
        assert parse_and_verify_rating(ev) is None
