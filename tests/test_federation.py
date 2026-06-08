"""Tests for federated reputation attestations (Federation #1).

Trust model: a rating is signed by the *payer* key and carries a *provider*
signature binding that payer key to the payment. These tests prove the two
attacks the original (preimage-only) model allowed are now dead, and document
the one accepted residual (provider self-dealing).

They also cover the phase-3 transport hardening done before federation goes
live: verify-once (verify_attestations feeds aggregate_reputation /
compute_payer_trust, which no longer re-verify), the SSRF guard on relay URLs
(_safe_relays / validate_relay_url), and the client-side event-intake cap.
"""

import pytest

from conduit.services.federation import (
    CONDUIT_RATING_KIND,
    DEFAULT_RATING_RELAYS,
    ReputationAttestation,
    _safe_relays,
    aggregate_reputation,
    attestation_matches_execution,
    build_rating_attestation,
    compute_payer_trust,
    dedupe_events,
    fetch_ratings,
    is_pubkey_hex,
    mint_execution_binding,
    parse_and_verify_rating,
    publish_rating,
    ratings_filter,
    sign_payer_binding,
    verify_attestations,
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


def _aggregate(events, provider_pubkey, *, payer_trust=None):
    """Verify-once then aggregate — the wired call shape (events -> attestations)."""
    return aggregate_reputation(
        skill_id=SKILL_ID,
        provider_pubkey=provider_pubkey,
        attestations=verify_attestations(events),
        payer_trust=payer_trust,
    )


class TestAggregation:
    def test_empty(self):
        prov = NostrKeypair.generate()
        agg = _aggregate([], prov.pubkey_hex)
        assert agg.score == 0.0 and agg.distinct_payers == 0 and agg.total_ratings == 0

    def test_single_rating(self):
        prov, p = NostrKeypair.generate(), NostrKeypair.generate()
        agg = _aggregate([_att(prov, p, 5, _h(1))], prov.pubkey_hex)
        assert agg.score == 5.0 and agg.distinct_payers == 1 and agg.total_ratings == 1

    def test_average_of_distinct_payers(self):
        prov = NostrKeypair.generate()
        evs = [_att(prov, NostrKeypair.generate(), s, _h(i)) for i, s in enumerate([5, 3], 1)]
        agg = _aggregate(evs, prov.pubkey_hex)
        assert agg.score == 4.0 and agg.distinct_payers == 2 and agg.total_ratings == 2

    def test_repeat_rater_diminishing_weight(self):
        prov, p = NostrKeypair.generate(), NostrKeypair.generate()
        evs = [_att(prov, p, 5, _h(1), created_at=1), _att(prov, p, 1, _h(2), created_at=2)]
        agg = _aggregate(evs, prov.pubkey_hex)
        assert agg.score == 3.67  # (5*1 + 1*0.5) / 1.5
        assert agg.distinct_payers == 1 and agg.total_ratings == 2

    def test_self_rating_excluded(self):
        prov, p = NostrKeypair.generate(), NostrKeypair.generate()
        evs = [_att(prov, prov, 5, _h(1)), _att(prov, p, 4, _h(2))]
        agg = _aggregate(evs, prov.pubkey_hex)
        assert agg.score == 4.0  # the provider's self-5 is excluded
        assert agg.self_ratings == 1 and agg.distinct_payers == 1
        assert "self_ratings_present" in agg.flags

    def test_only_self_ratings(self):
        prov = NostrKeypair.generate()
        agg = _aggregate([_att(prov, prov, 5, _h(1))], prov.pubkey_hex)
        assert agg.score == 0.0 and agg.total_ratings == 0 and agg.self_ratings == 1
        assert "no_independent_ratings" in agg.flags

    def test_dedupe_benign_rebroadcast(self):
        # Same rater + same score on one payment_hash is a re-broadcast, not an
        # attack: deduped to one, NOT flagged.
        prov, p = NostrKeypair.generate(), NostrKeypair.generate()
        evs = [_att(prov, p, 5, _h(1)), _att(prov, p, 5, _h(1))]
        agg = _aggregate(evs, prov.pubkey_hex)
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
        agg = _aggregate(evs, prov.pubkey_hex)
        assert "duplicate_payment_binding" in agg.flags
        assert agg.score == 1.0       # honest 1 survives, backdated 5 does not
        assert agg.total_ratings == 1

    def test_wrong_provider_excluded(self):
        prov, other, p = (NostrKeypair.generate() for _ in range(3))
        ev = _att(other, p, 5, _h(1))  # attestation is for a different provider
        agg = _aggregate([ev], prov.pubkey_hex)
        assert agg.total_ratings == 0

    def test_concentration_flag(self):
        prov, whale = NostrKeypair.generate(), NostrKeypair.generate()
        evs = [_att(prov, whale, 5, _h(i), created_at=i) for i in range(1, 4)]
        evs.append(_att(prov, NostrKeypair.generate(), 4, _h(9)))
        agg = _aggregate(evs, prov.pubkey_hex)
        assert "rating_concentration" in agg.flags  # 3 of 4 from one key

    def test_payer_trust_downweights_suspect_key(self):
        prov, trusted, sock = (NostrKeypair.generate() for _ in range(3))
        evs = [_att(prov, trusted, 1, _h(1)), _att(prov, sock, 5, _h(2))]
        plain = _aggregate(evs, prov.pubkey_hex)
        assert plain.score == 3.0  # (1 + 5) / 2
        weighted = _aggregate(
            evs, prov.pubkey_hex,
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
        trust = compute_payer_trust(verify_attestations(events))
        assert trust[diverse.pubkey_hex] == 1.0  # rates across 3 providers
        assert trust[narrow.pubkey_hex] == 0.5   # only one provider


class TestVerifyOnce:
    """Verify-once: verify_attestations is the single verification boundary."""

    def test_keeps_valid_drops_invalid(self):
        prov, a, b = (NostrKeypair.generate() for _ in range(3))
        good1, good2 = _att(prov, a, 5, _h(1)), _att(prov, b, 3, _h(2))
        bad = _att(prov, a, 4, _h(3))
        bad.sig = "00" * 64  # tamper -> fails the payer's event signature
        atts = verify_attestations([good1, bad, good2])
        assert {a_.payment_hash for a_ in atts} == {_h(1), _h(2)}  # the bad one dropped

    def test_aggregate_and_trust_consume_parsed_attestations(self):
        # The wired shape: verify once, hand the SAME parsed list to both consumers.
        prov_a, prov_b = NostrKeypair.generate(), NostrKeypair.generate()
        rater = NostrKeypair.generate()
        atts = verify_attestations([_att(prov_a, rater, 5, _h(1)), _att(prov_b, rater, 4, _h(2))])
        assert compute_payer_trust(atts)[rater.pubkey_hex] == 0.75  # 2 providers
        agg = aggregate_reputation(
            skill_id=SKILL_ID, provider_pubkey=prov_a.pubkey_hex, attestations=atts
        )
        assert agg.total_ratings == 1 and agg.score == 5.0

    def test_consumers_do_not_reverify(self, monkeypatch):
        import conduit.services.federation as fed

        prov, p = NostrKeypair.generate(), NostrKeypair.generate()
        events = [_att(prov, p, 5, _h(1))]
        calls = {"n": 0}
        real = fed.parse_and_verify_rating

        def counting(ev):
            calls["n"] += 1
            return real(ev)

        monkeypatch.setattr(fed, "parse_and_verify_rating", counting)
        atts = verify_attestations(events)
        assert calls["n"] == 1  # verified exactly once, here
        aggregate_reputation(skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex, attestations=atts)
        compute_payer_trust(atts)
        assert calls["n"] == 1  # neither consumer re-verified


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


class TestTransport:
    def test_ratings_filter(self):
        prov = NostrKeypair.generate()
        f = ratings_filter(prov.pubkey_hex, limit=10)
        assert f["kinds"] == [CONDUIT_RATING_KIND]
        assert f["#p"] == [prov.pubkey_hex]  # indexed single-letter provider tag
        assert f["limit"] == 10
        assert "since" not in f
        assert "since" in ratings_filter(prov.pubkey_hex, since_hours=24)

    def test_dedupe_events(self):
        prov, a, b, c = (NostrKeypair.generate() for _ in range(4))
        e1, e2, e3 = _att(prov, a, 5, _h(1)), _att(prov, b, 4, _h(2)), _att(prov, c, 3, _h(3))
        merged = dedupe_events([[e1, e2], [e2, e3]])  # e2 returned by two relays
        assert {e.id for e in merged} == {e1.id, e2.id, e3.id}

    async def test_publish_rating_rejects_non_rating(self):
        ev = NostrEvent(kind=1, tags=[], content="hi")
        ev.sign(NostrKeypair.generate())
        with pytest.raises(ValueError, match="rating"):
            await publish_rating(ev, ["wss://a"])  # kind guard fires before any dial

    async def test_publish_rating_delegates(self, monkeypatch):
        import conduit.services.federation as fed

        captured = {}

        async def fake_publish(event, urls, timeout=10.0):
            captured["event"], captured["urls"] = event, urls
            return {u: True for u in urls}

        monkeypatch.setattr(fed, "publish_to_relays", fake_publish)
        ev, _, _ = _attestation(score=5)
        res = await publish_rating(ev, ["wss://a", "wss://b"], validate_relays=False)
        assert captured["event"] is ev and captured["urls"] == ["wss://a", "wss://b"]
        assert res == {"wss://a": True, "wss://b": True}

    async def test_fetch_ratings_dedupes_then_aggregates(self, monkeypatch):
        import conduit.services.nostr as nostr_mod

        prov, a, b = (NostrKeypair.generate() for _ in range(3))
        e1, e2 = _att(prov, a, 5, _h(1)), _att(prov, b, 3, _h(2))
        canned = {"wss://x": [e1, e2], "wss://y": [e2]}  # e2 on both relays

        class FakeRelay:
            def __init__(self, url, timeout=10.0):
                self.url = url

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def subscribe(self, filters, sub_id="", max_events=None):
                return canned.get(self.url, [])

        monkeypatch.setattr(nostr_mod, "NostrRelay", FakeRelay)
        evs = await fetch_ratings(prov.pubkey_hex, ["wss://x", "wss://y"], validate_relays=False)
        assert {e.id for e in evs} == {e1.id, e2.id}  # deduped
        agg = aggregate_reputation(
            skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex,
            attestations=verify_attestations(evs),  # verify once, at the boundary
        )
        assert agg.total_ratings == 2 and agg.score == 4.0  # fetch -> verify -> aggregate

    async def test_fetch_ratings_tolerates_relay_errors(self, monkeypatch):
        import conduit.services.nostr as nostr_mod

        prov, a = NostrKeypair.generate(), NostrKeypair.generate()
        e1 = _att(prov, a, 5, _h(1))

        class FakeRelay:
            def __init__(self, url, timeout=10.0):
                self.url = url

            async def __aenter__(self):
                if self.url == "wss://bad":
                    raise OSError("connection refused")
                return self

            async def __aexit__(self, *a):
                return False

            async def subscribe(self, filters, sub_id="", max_events=None):
                return [e1]

        monkeypatch.setattr(nostr_mod, "NostrRelay", FakeRelay)
        evs = await fetch_ratings(
            prov.pubkey_hex, ["wss://bad", "wss://good"], validate_relays=False
        )
        assert {e.id for e in evs} == {e1.id}  # bad relay swallowed, good one returned

    async def test_fetch_ratings_caps_each_relay(self, monkeypatch):
        import conduit.services.nostr as nostr_mod

        prov = NostrKeypair.generate()
        seen = {}

        class FakeRelay:
            def __init__(self, url, timeout=10.0):
                self.url = url

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def subscribe(self, filters, sub_id="", max_events=None):
                seen["max_events"] = max_events
                return []

        monkeypatch.setattr(nostr_mod, "NostrRelay", FakeRelay)
        await fetch_ratings(prov.pubkey_hex, ["wss://x"], limit=7, validate_relays=False)
        assert seen["max_events"] == 7  # client-side cap threaded down per relay


class TestRelaySSRF:
    """SSRF guard on relay URLs — relevant once relays come from untrusted input."""

    def test_default_relays_pass_without_network(self):
        # The trusted defaults short-circuit the allowlist: kept as-is, no DNS.
        assert _safe_relays(list(DEFAULT_RATING_RELAYS)) == list(DEFAULT_RATING_RELAYS)

    def test_safe_relays_drops_internal_and_plaintext(self):
        # All literal IPs / scheme checks — no DNS, no network.
        urls = [
            "wss://1.1.1.1",          # public -> kept
            "wss://127.0.0.1",        # loopback -> dropped
            "wss://10.0.0.1",         # RFC1918 -> dropped
            "wss://169.254.169.254",  # cloud metadata -> dropped
            "ws://1.1.1.1",           # plaintext scheme -> dropped
        ]
        assert _safe_relays(urls) == ["wss://1.1.1.1"]

    async def test_fetch_ratings_does_not_dial_unsafe_relays(self, monkeypatch):
        import conduit.services.nostr as nostr_mod

        prov = NostrKeypair.generate()
        connected: list[str] = []

        class FakeRelay:
            def __init__(self, url, timeout=10.0):
                connected.append(url)  # records every dial attempt
                self.url = url

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def subscribe(self, filters, sub_id="", max_events=None):
                return []

        monkeypatch.setattr(nostr_mod, "NostrRelay", FakeRelay)
        # validate_relays defaults True; literal IPs keep this network-free.
        await fetch_ratings(prov.pubkey_hex, ["wss://127.0.0.1", "wss://1.1.1.1"])
        assert connected == ["wss://1.1.1.1"]  # the loopback relay was never dialed

    async def test_publish_rating_drops_unsafe_relays(self, monkeypatch):
        import conduit.services.federation as fed

        captured = {}

        async def fake_publish(event, urls, timeout=10.0):
            captured["urls"] = urls
            return {u: True for u in urls}

        monkeypatch.setattr(fed, "publish_to_relays", fake_publish)
        ev, _, _ = _attestation(score=5)
        await publish_rating(ev, ["wss://127.0.0.1", "wss://1.1.1.1"])  # default validation
        assert captured["urls"] == ["wss://1.1.1.1"]


class TestPubkeyValidation:
    def test_is_pubkey_hex(self):
        assert is_pubkey_hex("ab" * 32) is True   # 64 lowercase hex
        assert is_pubkey_hex("AB" * 32) is True   # uppercase accepted
        assert is_pubkey_hex("ab" * 31) is False  # 62 chars, too short
        assert is_pubkey_hex("zz" * 32) is False  # right length, not hex
        assert is_pubkey_hex("") is False
        assert is_pubkey_hex(None) is False        # non-str guard


class TestMintExecutionBinding:
    def test_mints_verifiable_binding(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        sig = mint_execution_binding(
            skill_id=SKILL_ID, payment_hash=PAYMENT_HASH, payer_pubkey=payer.pubkey_hex,
            provider_keypair=prov,
        )
        assert sig is not None
        assert verify_payer_binding(
            skill_id=SKILL_ID, payment_hash=PAYMENT_HASH, payer_pubkey=payer.pubkey_hex,
            provider_pubkey=prov.pubkey_hex, binding_sig=sig,
        )

    def test_none_without_payer_pubkey(self):
        prov = NostrKeypair.generate()
        assert mint_execution_binding(
            skill_id=SKILL_ID, payment_hash=PAYMENT_HASH, payer_pubkey=None,
            provider_keypair=prov,
        ) is None

    def test_none_when_disabled(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        assert mint_execution_binding(
            skill_id=SKILL_ID, payment_hash=PAYMENT_HASH, payer_pubkey=payer.pubkey_hex,
            provider_keypair=prov, enabled=False,
        ) is None


class TestAttestationMatchesExecution:
    """The anti-laundering guard: a rating must belong to the exact execution."""

    def _att(self, **over):
        base = dict(
            skill_id=SKILL_ID, provider_pubkey="p" * 64, rater_pubkey="r" * 64,
            payment_hash=PAYMENT_HASH, score=5, created_at=1,
        )
        base.update(over)
        return ReputationAttestation(**base)

    def test_matches(self):
        assert attestation_matches_execution(
            self._att(), skill_id=SKILL_ID, provider_pubkey="p" * 64,
            payment_hash=PAYMENT_HASH, payer_pubkey="r" * 64,
        )

    def test_rejects_each_mismatch(self):
        att = self._att()
        kw = dict(
            skill_id=SKILL_ID, provider_pubkey="p" * 64,
            payment_hash=PAYMENT_HASH, payer_pubkey="r" * 64,
        )
        assert not attestation_matches_execution(att, **{**kw, "skill_id": "other"})
        assert not attestation_matches_execution(att, **{**kw, "provider_pubkey": "x" * 64})
        assert not attestation_matches_execution(att, **{**kw, "payment_hash": "b" * 64})
        assert not attestation_matches_execution(att, **{**kw, "payer_pubkey": "z" * 64})
