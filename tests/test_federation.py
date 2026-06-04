"""Tests for federated reputation attestations (Federation #1, phase 1).

Trust model: a rating is signed by the *payer* key and carries a *provider*
signature binding that payer key to the payment. These tests prove the two
attacks the original (preimage-only) model allowed are now dead, and document
the one accepted residual (provider self-dealing).
"""

import pytest

from conduit.services.federation import (
    CONDUIT_RATING_KIND,
    build_rating_attestation,
    parse_and_verify_rating,
    sign_payer_binding,
    verify_payer_binding,
)
from conduit.services.nostr import NostrEvent, NostrKeypair

SKILL_ID = "11111111-1111-1111-1111-111111111111"
PAYMENT_HASH = "a" * 64


def _binding(provider: NostrKeypair, payer: NostrKeypair) -> str:
    return sign_payer_binding(
        skill_id=SKILL_ID, payment_hash=PAYMENT_HASH,
        payer_pubkey=payer.pubkey_hex, provider_keypair=provider,
    )


def _attestation(provider=None, payer=None, score=5):
    provider = provider or NostrKeypair.generate()
    payer = payer or NostrKeypair.generate()
    ev = build_rating_attestation(
        skill_id=SKILL_ID, provider_pubkey=provider.pubkey_hex, payment_hash=PAYMENT_HASH,
        score=score, payer_keypair=payer, provider_binding_sig=_binding(provider, payer),
    )
    return ev, provider, payer


class TestPayerBinding:
    def test_round_trip(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        assert verify_payer_binding(
            skill_id=SKILL_ID, payment_hash=PAYMENT_HASH, payer_pubkey=payer.pubkey_hex,
            provider_pubkey=prov.pubkey_hex, binding_sig=_binding(prov, payer),
        )

    def test_wrong_provider_rejected(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        sig = _binding(prov, payer)
        assert not verify_payer_binding(
            skill_id=SKILL_ID, payment_hash=PAYMENT_HASH, payer_pubkey=payer.pubkey_hex,
            provider_pubkey=NostrKeypair.generate().pubkey_hex, binding_sig=sig,
        )

    def test_wrong_payer_rejected(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        sig = _binding(prov, payer)
        assert not verify_payer_binding(
            skill_id=SKILL_ID, payment_hash=PAYMENT_HASH,
            payer_pubkey=NostrKeypair.generate().pubkey_hex,
            provider_pubkey=prov.pubkey_hex, binding_sig=sig,
        )

    def test_garbage(self):
        assert not verify_payer_binding(
            skill_id="s", payment_hash="h", payer_pubkey="x",
            provider_pubkey="y", binding_sig="z",
        )


class TestBuildAndVerify:
    def test_round_trip(self):
        ev, prov, payer = _attestation(score=4)
        assert ev.kind == CONDUIT_RATING_KIND
        assert ev.pubkey == payer.pubkey_hex
        att = parse_and_verify_rating(ev)
        assert att is not None
        assert att.skill_id == SKILL_ID
        assert att.score == 4
        assert att.rater_pubkey == payer.pubkey_hex
        assert att.provider_pubkey == prov.pubkey_hex
        assert att.dedupe_key == PAYMENT_HASH

    def test_cross_node_verify(self):
        ev, _, _ = _attestation()
        assert parse_and_verify_rating(ev) is not None  # only the public event needed

    def test_build_rejects_bad_score(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        binding = _binding(prov, payer)
        for bad in (0, 6):
            with pytest.raises(ValueError, match="score"):
                build_rating_attestation(
                    skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex,
                    payment_hash=PAYMENT_HASH, score=bad, payer_keypair=payer,
                    provider_binding_sig=binding,
                )

    def test_build_rejects_malformed_payment_hash(self):
        # build fails fast on what verify would later reject (no dead attestations).
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        bad = "xyz"
        binding = sign_payer_binding(
            skill_id=SKILL_ID, payment_hash=bad, payer_pubkey=payer.pubkey_hex,
            provider_keypair=prov,
        )
        with pytest.raises(ValueError, match="payment_hash"):
            build_rating_attestation(
                skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, payment_hash=bad,
                score=5, payer_keypair=payer, provider_binding_sig=binding,
            )

    def test_build_rejects_binding_for_other_payer(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        other = NostrKeypair.generate()
        with pytest.raises(ValueError, match="bind"):
            build_rating_attestation(
                skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex,
                payment_hash=PAYMENT_HASH, score=5, payer_keypair=payer,
                provider_binding_sig=_binding(prov, other),  # binding names a different payer
            )


class TestAttacksAreDead:
    def test_provider_cannot_forge_customers_rating(self):
        # Provider knows the customer pubkey (from the request) and can sign a
        # binding for it, but cannot sign the RATING as the customer.
        prov = NostrKeypair.generate()
        customer = NostrKeypair.generate()
        ev = NostrEvent(
            kind=CONDUIT_RATING_KIND,
            tags=[["skill", SKILL_ID], ["p", prov.pubkey_hex], ["payment_hash", PAYMENT_HASH],
                  ["score", "5"], ["provider_binding", _binding(prov, customer)]],
            content="",
        )
        ev.sign(prov)  # signed by the provider, not the customer
        assert parse_and_verify_rating(ev) is None  # payer (provider) != bound payer (customer)

    def test_takeover_rejected(self):
        # Mallory grabs a published attestation and re-signs it to contest the slot.
        ev, _, _ = _attestation(score=1)
        forged = NostrEvent(kind=CONDUIT_RATING_KIND, tags=[t[:] for t in ev.tags], content="")
        forged.sign(NostrKeypair.generate())  # Mallory's key
        assert parse_and_verify_rating(forged) is None  # binding is for the real payer

    def test_self_deal_is_accepted_residual(self):
        # Documented residual: provider self-dealing is not cryptographically
        # preventable (the provider can always be the payer). Here one key plays
        # both provider and payer over a fabricated hash, and verify still accepts
        # it. Defended at the aggregation layer (distinct-payer weighting,
        # provider == payer exclusion, payer web-of-trust), not by crypto.
        sock = NostrKeypair.generate()
        ev, _, _ = _attestation(provider=sock, payer=sock)
        assert parse_and_verify_rating(ev) is not None  # accepted by design


class TestMalformed:
    def test_tampered_score_rejected(self):
        ev, _, _ = _attestation(score=1)
        for t in ev.tags:
            if t[0] == "score":
                t[1] = "5"
        assert parse_and_verify_rating(ev) is None

    def test_forged_signature_rejected(self):
        ev, _, _ = _attestation()
        ev.sig = "00" * 64
        assert parse_and_verify_rating(ev) is None

    def test_duplicate_required_tag_rejected(self):
        ev, _, payer = _attestation()
        ev.tags.append(["score", "1"])
        ev.sign(payer)  # re-sign so the sig is valid over the duplicated tags
        assert parse_and_verify_rating(ev) is None

    def test_wrong_kind_rejected(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        ev = NostrEvent(
            kind=1,
            tags=[["skill", SKILL_ID], ["p", prov.pubkey_hex], ["payment_hash", PAYMENT_HASH],
                  ["score", "5"], ["provider_binding", _binding(prov, payer)]],
            content="",
        )
        ev.sign(payer)
        assert parse_and_verify_rating(ev) is None

    def test_missing_tag_rejected(self):
        payer = NostrKeypair.generate()
        ev = NostrEvent(kind=CONDUIT_RATING_KIND, tags=[["score", "5"]], content="")
        ev.sign(payer)
        assert parse_and_verify_rating(ev) is None
