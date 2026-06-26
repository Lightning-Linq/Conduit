"""Tests for the federated skill-catalog cache ingest layer (Federation #2, Task 2).

DB round-trips need Postgres (conftest mocks the DB), so these cover the pure trust
boundary: signature re-verify on ingest, self-exclusion of our own listings, skill
parsing, and newest-wins de-dup per (provider_pubkey, skill_id) coordinate.
"""

from conduit.services.federation_catalog import (
    _skill_row_values,
    _skill_rows_from_events,
)
from conduit.services.nostr import (
    NostrEvent,
    NostrKeypair,
    event_to_skill,
    skill_to_event,
)

SK1 = "11111111-1111-1111-1111-111111111111"
SK2 = "22222222-2222-2222-2222-222222222222"

# A self_pubkey that matches none of the random provider keys below (excludes nothing).
OTHER = NostrKeypair.generate().pubkey_hex


def _skill_event(
    keypair, *, skill_id=SK1, name="Test Skill", category="data",
    price_sats=100, created_at=1000,
):
    """A properly signed kind-38383 skill listing with a deterministic created_at."""
    skill = {
        "id": skill_id, "name": name, "category": category, "price_sats": price_sats,
        "description": "a skill", "provider_name": "Prov",
        "provider_lightning_address": "prov@ln.tld",
        "endpoint_url": "https://example.com/api", "tags": "ai,data",
    }
    ev = skill_to_event(skill, keypair)
    ev.created_at = created_at
    ev.sign(keypair)  # re-sign so id+sig match the chosen created_at
    return ev


class TestRowMapping:
    def test_row_values_capture_event_and_skill(self):
        prov = NostrKeypair.generate()
        ev = _skill_event(prov, skill_id=SK1, name="Indexer", price_sats=250)
        row = _skill_row_values(
            ev, event_to_skill(ev), origin="peer", source_id="https://peerB"
        )
        assert row["provider_pubkey"] == prov.pubkey_hex  # signer == provider
        assert row["skill_id"] == SK1
        assert row["event_id"] == ev.id
        assert row["event_created_at"] == 1000
        assert row["name"] == "Indexer"
        assert row["price_sats"] == 250
        assert row["origin"] == "peer"
        assert row["source_id"] == "https://peerB"
        assert row["raw_event"] == ev.to_dict()  # full event kept for re-verify


class TestTrustBoundary:
    def test_keeps_only_signature_verified_listings(self):
        prov = NostrKeypair.generate()
        good = _skill_event(prov, skill_id=SK1)
        tampered = _skill_event(prov, skill_id=SK2)
        tampered.content = tampered.content + "X"  # breaks id->sig; verify() now False
        rows = _skill_rows_from_events([good, tampered], self_pubkey=OTHER)
        assert [r["skill_id"] for r in rows] == [SK1]

    def test_wrong_kind_event_dropped(self):
        bad = NostrEvent(kind=1, tags=[["d", SK1]], content="{}")
        bad.sign(NostrKeypair.generate())
        assert _skill_rows_from_events([bad], self_pubkey=OTHER) == []

    def test_empty_skill_id_dropped(self):
        prov = NostrKeypair.generate()
        ev = _skill_event(prov, skill_id="")  # no usable (provider, skill) coordinate
        assert _skill_rows_from_events([ev], self_pubkey=OTHER) == []

    def test_self_exclusion_drops_own_listings(self):
        me = NostrKeypair.generate()
        mine = _skill_event(me, skill_id=SK1)
        # Signed by us -> never ingest our own catalog echoed back by a peer/relay.
        assert _skill_rows_from_events([mine], self_pubkey=me.pubkey_hex) == []
        # A different signer's same-coordinate listing IS kept.
        other = NostrKeypair.generate()
        theirs = _skill_event(other, skill_id=SK1)
        rows = _skill_rows_from_events([theirs], self_pubkey=me.pubkey_hex)
        assert len(rows) == 1 and rows[0]["provider_pubkey"] == other.pubkey_hex


class TestNewestWins:
    def test_newest_event_wins_within_batch(self):
        prov = NostrKeypair.generate()
        old = _skill_event(prov, skill_id=SK1, name="Old", created_at=1000)
        new = _skill_event(prov, skill_id=SK1, name="New", created_at=2000)
        for batch in ([old, new], [new, old]):  # order-independent
            rows = _skill_rows_from_events(batch, self_pubkey=OTHER)
            assert len(rows) == 1
            assert rows[0]["event_created_at"] == 2000
            assert rows[0]["name"] == "New"

    def test_distinct_coordinates_both_kept(self):
        prov = NostrKeypair.generate()
        a = _skill_event(prov, skill_id=SK1)
        b = _skill_event(prov, skill_id=SK2)
        rows = _skill_rows_from_events([a, b], self_pubkey=OTHER)
        assert {r["skill_id"] for r in rows} == {SK1, SK2}


class TestSizeCap:
    def test_oversized_event_dropped_on_ingest(self):
        """A signed-but-huge listing is dropped whole (DB-bloat guard), not truncated;
        a normal event in the same batch is kept verbatim."""
        from conduit.services.federation_catalog import _MAX_EVENT_BYTES

        prov = NostrKeypair.generate()
        normal = _skill_event(prov, skill_id=SK1)
        big = skill_to_event(
            {
                "id": SK2, "name": "Big", "category": "data", "price_sats": 1,
                "description": "x" * (_MAX_EVENT_BYTES + 1),  # blows past the cap
                "provider_name": "Prov", "provider_lightning_address": "",
                "endpoint_url": "", "tags": "",
            },
            prov,
        )
        # It IS oversize and otherwise valid — dropped for size, not a bad signature.
        assert len(big.serialize_for_id()) > _MAX_EVENT_BYTES
        assert big.verify()

        rows = _skill_rows_from_events([normal, big], self_pubkey=OTHER)
        assert [r["skill_id"] for r in rows] == [SK1]  # oversize dropped, normal kept
        # drop-not-truncate: the kept event is stored verbatim (raw_event unchanged).
        assert rows[0]["raw_event"] == normal.to_dict()
