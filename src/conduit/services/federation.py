"""
Federated reputation — payer-signed, provider-bound rating attestations.

Federation #1 (shared trust layer). A rating crosses node boundaries as a
Nostr event that two parties have signed:

  * the **payer** signs the rating (the event signature) with the key they
    established when requesting the execution, and
  * the **provider** (the skill's owner) signs a *binding* attesting that
    "payer_pubkey paid payment_hash for skill_id" (the `provider_binding` tag).

Any node verifies both signatures against the skill's known provider key. This
binds a rating to the specific paying key, which kills the two attacks a bare
preimage allowed:

  - a provider cannot forge a *real customer's* rating (it has no payer key), and
  - a published attestation cannot be re-attributed by a third party (a forger
    would have to sign as the payer, or mint a provider binding for its own key).

The raw preimage is never published: it is a bearer token the payee also holds,
so it proved nothing about *who* rated, and publishing it leaks the provider's
payment graph.

Residual (accepted for v1): a provider can self-deal with its own sock-puppet
key, but that requires a real Lightning payment and trips the existing
self-payment / concentration anomaly detection. BOLT12 payer_key binding is the
trustless long-term hardening.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

from conduit.services.nostr import (
    NostrEvent,
    NostrKeypair,
    _schnorr_sign,
    _schnorr_verify,
)

# Conduit-specific Nostr event kind. Regular range (1000-9999) so relays keep
# every event — ratings accumulate, they do not replace one another.
CONDUIT_RATING_KIND = 9070

_TAG_SKILL = "skill"
_TAG_PROVIDER = "p"
_TAG_PAYMENT_HASH = "payment_hash"
_TAG_SCORE = "score"
_TAG_BINDING = "provider_binding"
_REQUIRED_TAGS = (_TAG_SKILL, _TAG_PROVIDER, _TAG_PAYMENT_HASH, _TAG_SCORE, _TAG_BINDING)


@dataclass
class ReputationAttestation:
    """The parsed, fully-verified view of a rating attestation."""

    skill_id: str
    provider_pubkey: str
    rater_pubkey: str
    payment_hash: str
    score: int
    created_at: int

    @property
    def dedupe_key(self) -> str:
        """One rating counts per real payment."""
        return self.payment_hash


def _binding_message(skill_id: str, payment_hash: str, payer_pubkey: str) -> bytes:
    """The 32-byte message a provider signs to bind a payer key to a payment."""
    raw = f"conduit-pay-binding:v1:{skill_id}:{payment_hash}:{payer_pubkey}"
    return hashlib.sha256(raw.encode("utf-8")).digest()


def sign_payer_binding(
    *, skill_id: str, payment_hash: str, payer_pubkey: str, provider_keypair: NostrKeypair
) -> str:
    """Provider attests payer_pubkey paid payment_hash for skill_id. Returns a hex sig."""
    msg = _binding_message(skill_id, payment_hash, payer_pubkey)
    sig = _schnorr_sign(msg, bytes.fromhex(provider_keypair.privkey_hex), secrets.token_bytes(32))
    return sig.hex()


def verify_payer_binding(
    *, skill_id: str, payment_hash: str, payer_pubkey: str, provider_pubkey: str, binding_sig: str
) -> bool:
    """True iff binding_sig is the provider's signature binding payer_pubkey to the payment."""
    try:
        msg = _binding_message(skill_id, payment_hash, payer_pubkey)
        return _schnorr_verify(msg, bytes.fromhex(provider_pubkey), bytes.fromhex(binding_sig))
    except (ValueError, TypeError):
        return False


def build_rating_attestation(
    *,
    skill_id: str,
    provider_pubkey: str,
    payment_hash: str,
    score: int,
    payer_keypair: NostrKeypair,
    provider_binding_sig: str,
    created_at: int = 0,
) -> NostrEvent:
    """Build a payer-signed rating event carrying the provider's payer-binding.

    Raises ValueError on a bad score, or if the binding does not actually bind
    this payer key to this payment (so we never publish an inconsistent record).
    """
    if not 1 <= score <= 5:
        raise ValueError(f"score must be 1..5, got {score}")
    if not verify_payer_binding(
        skill_id=skill_id,
        payment_hash=payment_hash,
        payer_pubkey=payer_keypair.pubkey_hex,
        provider_pubkey=provider_pubkey,
        binding_sig=provider_binding_sig,
    ):
        raise ValueError("provider_binding_sig does not bind this payer to this payment")

    event = NostrEvent(
        kind=CONDUIT_RATING_KIND,
        created_at=created_at or int(time.time()),
        tags=[
            [_TAG_SKILL, skill_id],
            [_TAG_PROVIDER, provider_pubkey],
            [_TAG_PAYMENT_HASH, payment_hash],
            [_TAG_SCORE, str(score)],
            [_TAG_BINDING, provider_binding_sig],
        ],
        content="",
    )
    event.sign(payer_keypair)  # the rating signature is the payer's
    return event


def _required_tags(event: NostrEvent) -> dict[str, str] | None:
    """Return the required tag values, or None if any is missing or duplicated.

    Duplicate required tags are rejected so every node derives identical values
    — otherwise a 'first-wins' peer and a 'last-wins' peer could disagree on
    payment_hash/score and break cross-node dedup.
    """
    out: dict[str, str] = {}
    for tag in event.tags:
        if len(tag) < 2 or tag[0] not in _REQUIRED_TAGS:
            continue
        if tag[0] in out:
            return None
        out[tag[0]] = tag[1]
    if any(name not in out for name in _REQUIRED_TAGS):
        return None
    return out


def parse_and_verify_rating(event: NostrEvent) -> ReputationAttestation | None:
    """Verify a rating attestation; return the parsed view, or None.

    Checks: kind, the payer's Schnorr signature (the event sig), required tags
    present and unique, score range, and the provider's payer-binding signature
    over (skill_id, payment_hash, payer_pubkey). Any failure returns None.
    """
    if event.kind != CONDUIT_RATING_KIND:
        return None
    if not event.verify():  # rating sig by the payer (event.pubkey) + id integrity
        return None

    tags = _required_tags(event)
    if tags is None:
        return None

    try:
        score = int(tags[_TAG_SCORE])
    except ValueError:
        return None
    if not 1 <= score <= 5:
        return None

    payer_pubkey = event.pubkey
    provider_pubkey = tags[_TAG_PROVIDER]
    payment_hash = tags[_TAG_PAYMENT_HASH]
    if not verify_payer_binding(
        skill_id=tags[_TAG_SKILL],
        payment_hash=payment_hash,
        payer_pubkey=payer_pubkey,
        provider_pubkey=provider_pubkey,
        binding_sig=tags[_TAG_BINDING],
    ):
        return None

    return ReputationAttestation(
        skill_id=tags[_TAG_SKILL],
        provider_pubkey=provider_pubkey,
        rater_pubkey=payer_pubkey,
        payment_hash=payment_hash,
        score=score,
        created_at=event.created_at,
    )
