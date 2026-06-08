"""Tests for the federated reputation cache (Federation #1, phase 4).

DB round-trips need Postgres (conftest mocks the DB), so these cover the pure
layer: the verify-on-write trust boundary, row<->attestation conversion, and that
aggregating from cached rows yields the same result as aggregating a live fetch.
"""

from conduit.models.federated_attestation import FederatedAttestation
from conduit.services.federation import (
    CONDUIT_RATING_KIND,
    aggregate_reputation,
    build_rating_attestation,
    parse_and_verify_rating,
    sign_payer_binding,
    verify_attestations,
)
from conduit.services.federation_cache import (
    _row_to_attestation,
    _row_values,
    _rows_from_events,
)
from conduit.services.nostr import NostrEvent, NostrKeypair

SKILL_ID = "11111111-1111-1111-1111-111111111111"


def _h(i: int) -> str:
    return f"{i:064x}"


def _att(provider, payer, score, payment_hash, created_at=1000):
    binding = sign_payer_binding(
        skill_id=SKILL_ID, payment_hash=payment_hash,
        payer_pubkey=payer.pubkey_hex, provider_keypair=provider,
    )
    return build_rating_attestation(
        skill_id=SKILL_ID, provider_pubkey=provider.pubkey_hex, payment_hash=payment_hash,
        score=score, payer_keypair=payer, provider_binding_sig=binding, created_at=created_at,
    )


def _row(event: NostrEvent) -> FederatedAttestation:
    """A transient (no-DB) cache row built from an event, as store_events would."""
    att = parse_and_verify_rating(event)
    return FederatedAttestation(**_row_values(event, att))


class TestCacheConversion:
    def test_row_values_capture_event_and_attestation(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        ev = _att(prov, payer, 4, _h(1))
        vals = _row_values(ev, parse_and_verify_rating(ev))
        assert vals["event_id"] == ev.id
        assert vals["skill_id"] == SKILL_ID
        assert vals["provider_pubkey"] == prov.pubkey_hex
        assert vals["rater_pubkey"] == payer.pubkey_hex
        assert vals["payment_hash"] == _h(1)
        assert vals["score"] == 4
        assert vals["attestation_created_at"] == 1000
        assert vals["raw_event"] == ev.to_dict()  # full event kept for re-verify

    def test_row_round_trips_to_attestation(self):
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        row = _row(_att(prov, payer, 5, _h(1)))
        att = _row_to_attestation(row)
        assert att.skill_id == SKILL_ID
        assert att.provider_pubkey == prov.pubkey_hex
        assert att.rater_pubkey == payer.pubkey_hex
        assert att.payment_hash == _h(1)
        assert att.score == 5
        assert att.created_at == 1000

    def test_aggregate_from_cache_matches_live(self):
        # Aggregating cached rows == aggregating the live-fetched attestations.
        prov, a, b = (NostrKeypair.generate() for _ in range(3))
        evs = [_att(prov, a, 5, _h(1)), _att(prov, b, 3, _h(2))]
        live = aggregate_reputation(
            skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex,
            attestations=verify_attestations(evs),
        )
        cached = aggregate_reputation(
            skill_id=SKILL_ID, provider_pubkey=prov.pubkey_hex,
            attestations=[_row_to_attestation(_row(ev)) for ev in evs],
        )
        assert cached == live  # AggregateReputation is a dataclass: value equality


class TestTrustBoundary:
    def test_rows_from_events_keeps_only_verified(self):
        # Only events that pass parse_and_verify_rating become rows; an unverified
        # event (wrong kind here) is dropped, so nothing unverified is written.
        prov, payer = NostrKeypair.generate(), NostrKeypair.generate()
        good = _att(prov, payer, 5, _h(1))
        bad = NostrEvent(kind=1, tags=[], content="not a rating")
        bad.sign(NostrKeypair.generate())
        rows = _rows_from_events([good, bad])
        assert len(rows) == 1
        assert rows[0]["event_id"] == good.id

    def test_wrong_kind_constant_is_not_rating(self):
        # Guard the assumption above: kind 1 is not the rating kind.
        assert CONDUIT_RATING_KIND != 1
