"""Tests for federated reputation attestations (Federation #1, phase 1).

Trust model: a rating is signed by the *payer* key and carries a *provider*
signature binding that payer key to the payment. These tests prove the two
attacks the original (preimage-only) model allowed are now dead, and document
the one accepted residual (provider self-dealing).
"""

import pytest

from conduit.services.federation import (
    CONDUIT_RATING_KIND,
    aggregate_reputation,
    build_rating_attestation,
    compute_payer_trust,
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


def _h(i: int) -> str:
    """A distinct, valid 32-byte hex payment hash."""
    return f"{i:064x}"


def _att(provider, payer, score, payment_hash, created_at=1000):
    """Build one valid rating attestation (for aggregation tests)."""
    binding = sign_payer_binding(
        skill_id=SKILL_ID, payment_hash=payment_hash,
        payer_pubkey=payer.pubkey_hex, provider_keypair=provider,
    )
    return build_rating_attestation(
        skill_id=SKILL_ID, provider_pubkey=provider.pubkey_hex, payment_hash=payment_hash,
        score=score, payer_keypair=payer, provider_binding_sig=binding, created_at=created_at,
    )


class TestAggregation:
    def test_empty(self):
        prov = NostrKeypair.generate()
        agg = aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=[])
        assert agg.score == 0.0 and agg.distinct_payers == 0 and agg.total_ratings == 0

    def test_single_rating(self):
        prov, p = NostrKeypair.generate(), NostrKeypair.generate()
        agg = aggregate_reputation(
            skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=[_att(prov, p, 5, _h(1))]
        )
        assert agg.score == 5.0 and agg.distinct_payers == 1 and agg.total_ratings == 1

    def test_average_of_distinct_payers(self):
        prov = NostrKeypair.generate()
        evs = [_att(prov, NostrKeypair.generate(), s, _h(i)) for i, s in enumerate([5, 3], 1)]
        agg = aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=evs)
        assert agg.score == 4.0 and agg.distinct_payers == 2 and agg.total_ratings == 2

    def test_repeat_rater_diminishing_weight(self):
        prov, p = NostrKeypair.generate(), NostrKeypair.generate()
        evs = [_att(prov, p, 5, _h(1), created_at=1), _att(prov, p, 1, _h(2), created_at=2)]
        agg = aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=evs)
        assert agg.score == 3.67  # (5*1 + 1*0.5) / 1.5
        assert agg.distinct_payers == 1 and agg.total_ratings == 2

    def test_self_rating_excluded(self):
        prov, p = NostrKeypair.generate(), NostrKeypair.generate()
        evs = [_att(prov, prov, 5, _h(1)), _att(prov, p, 4, _h(2))]
        agg = aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=evs)
        assert agg.score == 4.0  # the provider's self-5 is excluded
        assert agg.self_ratings == 1 and agg.distinct_payers == 1
        assert "self_ratings_present" in agg.flags

    def test_only_self_ratings(self):
        prov = NostrKeypair.generate()
        agg = aggregate_reputation(
            skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=[_att(prov, prov, 5, _h(1))]
        )
        assert agg.score == 0.0 and agg.total_ratings == 0 and agg.self_ratings == 1
        assert "no_independent_ratings" in agg.flags

    def test_dedupe_benign_rebroadcast(self):
        # Same rater + same score on one payment_hash is a re-broadcast, not an
        # attack: deduped to one, NOT flagged.
        prov, p = NostrKeypair.generate(), NostrKeypair.generate()
        evs = [_att(prov, p, 5, _h(1)), _att(prov, p, 5, _h(1))]
        agg = aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=evs)
        assert agg.total_ratings == 1
        assert "duplicate_payment_binding" not in agg.flags

    def test_duplicate_payment_binding_flagged_lowest_kept(self):
        # A provider re-binds a real payment_hash to a sock puppet with a higher,
        # backdated rating. created_at must NOT win: the honest low rating survives
        # and the collision is flagged (no silent suppression).
        prov, alice, sock = (NostrKeypair.generate() for _ in range(3))
        evs = [
            _att(prov, alice, 1, _h(1), created_at=100),  # Alice's genuine 1
            _att(prov, sock, 5, _h(1), created_at=1),     # provider's backdated sock 5
        ]
        agg = aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=evs)
        assert "duplicate_payment_binding" in agg.flags
        assert agg.score == 1.0       # honest 1 survives, backdated 5 does not
        assert agg.total_ratings == 1

    def test_wrong_provider_excluded(self):
        prov, other, p = (NostrKeypair.generate() for _ in range(3))
        ev = _att(other, p, 5, _h(1))  # attestation is for a different provider
        agg = aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=[ev])
        assert agg.total_ratings == 0

    def test_concentration_flag(self):
        prov, whale = NostrKeypair.generate(), NostrKeypair.generate()
        evs = [_att(prov, whale, 5, _h(i), created_at=i) for i in range(1, 4)]
        evs.append(_att(prov, NostrKeypair.generate(), 4, _h(9)))
        agg = aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=evs)
        assert "rating_concentration" in agg.flags  # 3 of 4 from one key

    def test_payer_trust_downweights_suspect_key(self):
        prov, trusted, sock = (NostrKeypair.generate() for _ in range(3))
        evs = [_att(prov, trusted, 1, _h(1)), _att(prov, sock, 5, _h(2))]
        plain = aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=evs)
        assert plain.score == 3.0  # (1 + 5) / 2
        weighted = aggregate_reputation(
            skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, events=evs,
            payer_trust={trusted.pubkey_hex: 1.0, sock.pubkey_hex: 0.2},
        )
        assert weighted.score < plain.score  # the sock's 5 is down-weighted

    def test_compute_payer_trust_rewards_diversity(self):
        prov_a, prov_b, prov_c = (NostrKeypair.generate() for _ in range(3))
        diverse, narrow = NostrKeypair.generate(), NostrKeypair.generate()
        events = [
            _att(prov_a, diverse, 5, _h(1)), _att(prov_b, diverse, 5, _h(2)),
            _att(prov_c, diverse, 5, _h(3)), _att(prov_a, narrow, 5, _h(4)),
        ]
        trust = compute_payer_trust(events)
        assert trust[diverse.pubkey_hex] == 1.0  # rates across 3 providers
        assert trust[narrow.pubkey_hex] == 0.5   # only one provider


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
