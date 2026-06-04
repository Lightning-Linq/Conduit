"""
Federated reputation — signed, payment-proven rating attestations.

Federation #1 (shared trust layer): a rating is published as a signed Nostr
event that carries the Lightning *preimage* as proof of payment. Any node can
verify it offline, with no trust in the publisher:

  * sha256(preimage) == payment_hash  proves a real payment was made
  * Schnorr signature over the event  proves the rater's identity

Attestations are deduped by payment_hash (one rating per real payment), which
caps sybil attacks by payment cost. The module is transport-agnostic: an
attestation is a NostrEvent, so it can ride Nostr relays now and direct peer
sync later (#2) without changing the data or the verification path.

See FEDERATION_DESIGN.md.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from conduit.services.nostr import NostrEvent, NostrKeypair

# Conduit-specific Nostr event kinds. Regular range (1000-9999) so relays keep
# every event — ratings must accumulate, not replace one another.
CONDUIT_RATING_KIND = 9070


@dataclass
class ReputationAttestation:
    """The parsed, *verified* view of a rating attestation."""

    skill_id: str
    provider_pubkey: str
    rater_pubkey: str
    payment_hash: str
    score: int
    created_at: int

    @property
    def dedupe_key(self) -> str:
        """One rating counts per real payment (anti-sybil)."""
        return self.payment_hash


def verify_payment_proof(payment_hash: str, preimage: str) -> bool:
    """True iff sha256(preimage) == payment_hash (proves the invoice was paid)."""
    try:
        return hashlib.sha256(bytes.fromhex(preimage)).hexdigest() == payment_hash.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def build_rating_attestation(
    *,
    skill_id: str,
    provider_pubkey: str,
    payment_hash: str,
    payment_preimage: str,
    score: int,
    keypair: NostrKeypair,
    created_at: int = 0,
) -> NostrEvent:
    """Build and sign a rating attestation as a Nostr event.

    Raises ValueError on an out-of-range score or a preimage that does not prove
    the payment, so we never publish an unverifiable claim.
    """
    if not 1 <= score <= 5:
        raise ValueError(f"score must be 1..5, got {score}")
    if not verify_payment_proof(payment_hash, payment_preimage):
        raise ValueError("payment_preimage does not hash to payment_hash")

    event = NostrEvent(
        kind=CONDUIT_RATING_KIND,
        created_at=created_at,
        tags=[
            ["skill", skill_id],
            ["p", provider_pubkey],
            ["payment_hash", payment_hash],
            ["preimage", payment_preimage],
            ["score", str(score)],
        ],
        content="",
    )
    event.sign(keypair)
    return event


def parse_and_verify_rating(event: NostrEvent) -> ReputationAttestation | None:
    """Verify a rating attestation; return the parsed view, or None if invalid.

    Checks, in order: kind, Schnorr signature + event id, required tags, score
    range, and the payment proof. Any failure returns None so callers can simply
    drop the attestation.
    """
    if event.kind != CONDUIT_RATING_KIND:
        return None
    if not event.verify():
        return None

    tags = {t[0]: t[1] for t in event.tags if len(t) >= 2}
    skill_id = tags.get("skill")
    payment_hash = tags.get("payment_hash")
    preimage = tags.get("preimage")
    score_raw = tags.get("score")
    if not (skill_id and payment_hash and preimage and score_raw):
        return None

    try:
        score = int(score_raw)
    except ValueError:
        return None
    if not 1 <= score <= 5:
        return None
    if not verify_payment_proof(payment_hash, preimage):
        return None

    return ReputationAttestation(
        skill_id=skill_id,
        provider_pubkey=tags.get("p", ""),
        rater_pubkey=event.pubkey,
        payment_hash=payment_hash,
        score=score,
        created_at=event.created_at,
    )
