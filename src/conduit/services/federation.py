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

Residual (NOT prevented, and not cryptographically preventable): a provider can
self-deal by paying and rating its own skill. No payment-identity scheme stops
this, because the provider can always BE the payer (even BOLT12 payer_key only
proves a real payer key signed, which the provider can control; and NWC can't do
BOLT12 anyway). Self-dealing is an economic/sybil problem, not a crypto one. It is
defended at the aggregation layer (distinct-payer weighting, provider == payer /
cluster exclusion, payer web-of-trust, surfacing distinct-payer counts) plus the
real cost of settled payments, which reduces it to "costly and detectable", not
eliminated. Federated scores are "network-reported, weighted", not "verified", and
federation must not feed the live reputation path until those defenses exist.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from conduit.services.nostr import (
    NostrEvent,
    NostrKeypair,
    _subscribe_across_relays,
    build_req_filter,
    publish_to_relays,
    schnorr_sign,
    schnorr_verify,
)
from conduit.services.url_safety import UnsafeURLError, validate_relay_url

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


def _is_hex(value: str, n_bytes: int) -> bool:
    """True iff value is exactly n_bytes encoded as lowercase/uppercase hex."""
    if not isinstance(value, str) or len(value) != n_bytes * 2:
        return False
    try:
        bytes.fromhex(value)
        return True
    except ValueError:
        return False


def is_pubkey_hex(value: str) -> bool:
    """True iff value is a 32-byte x-only public key in hex (a Nostr pubkey)."""
    return _is_hex(value, 32)


def mint_execution_binding(
    *,
    skill_id: str,
    payment_hash: str | None,
    payer_pubkey: str | None,
    provider_keypair: NostrKeypair,
    enabled: bool = True,
) -> str | None:
    """The provider's payer-binding for a settled execution, or None.

    Returns None (not federatable) when federation is disabled or the execution
    has no captured payer_pubkey / payment_hash; otherwise signs the binding with
    the node key (see sign_payer_binding). Call ONLY after settlement is verified:
    the binding asserts the payment, so minting it without a real settled payment
    is exactly the provider self-deal the trust model accepts as residual.
    """
    if not enabled or not payer_pubkey or not payment_hash:
        return None
    return sign_payer_binding(
        skill_id=skill_id,
        payment_hash=payment_hash,
        payer_pubkey=payer_pubkey,
        provider_keypair=provider_keypair,
    )


def attestation_matches_execution(
    att: ReputationAttestation,
    *,
    skill_id: str,
    provider_pubkey: str,
    payment_hash: str,
    payer_pubkey: str,
) -> bool:
    """True iff a verified attestation belongs to this exact settled execution.

    Stops a caller from laundering an unrelated (but individually valid)
    attestation through submit_rating: the rating must be for this skill + provider
    + payment, and signed by the payer key captured at request time.
    """
    return (
        att.skill_id == skill_id
        and att.provider_pubkey == provider_pubkey
        and att.payment_hash == payment_hash
        and att.rater_pubkey == payer_pubkey
    )


def _binding_message(skill_id: str, payment_hash: str, payer_pubkey: str) -> bytes:
    """The 32-byte message a provider signs to bind a payer key to a payment.

    Fields are length-prefixed (not delimiter-joined) so no field value can shift
    the boundaries of the signed message.
    """
    fields = [
        b"conduit-pay-binding:v1",
        skill_id.encode("utf-8"),
        payment_hash.encode("utf-8"),
        payer_pubkey.encode("utf-8"),
    ]
    buf = b"".join(len(f).to_bytes(4, "big") + f for f in fields)
    return hashlib.sha256(buf).digest()


def sign_payer_binding(
    *, skill_id: str, payment_hash: str, payer_pubkey: str, provider_keypair: NostrKeypair
) -> str:
    """Provider attests payer_pubkey paid payment_hash for skill_id. Returns a hex sig."""
    msg = _binding_message(skill_id, payment_hash, payer_pubkey)
    sig = schnorr_sign(msg, bytes.fromhex(provider_keypair.privkey_hex), secrets.token_bytes(32))
    return sig.hex()


def verify_payer_binding(
    *, skill_id: str, payment_hash: str, payer_pubkey: str, provider_pubkey: str, binding_sig: str
) -> bool:
    """True iff binding_sig is the provider's signature binding payer_pubkey to the payment."""
    try:
        msg = _binding_message(skill_id, payment_hash, payer_pubkey)
        return schnorr_verify(msg, bytes.fromhex(provider_pubkey), bytes.fromhex(binding_sig))
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
    if not _is_hex(payment_hash, 32):
        raise ValueError("payment_hash must be 32-byte hex")
    if not _is_hex(provider_pubkey, 32):
        raise ValueError("provider_pubkey must be 32-byte hex")
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
    present and unique, well-formed payment_hash/provider key, score range, and
    the provider's payer-binding signature. Any failure returns None.
    """
    if event.kind != CONDUIT_RATING_KIND:
        return None
    if not event.verify():  # rating sig by the payer (event.pubkey) + id integrity
        return None

    tags = _required_tags(event)
    if tags is None:
        return None

    payment_hash = tags[_TAG_PAYMENT_HASH]
    provider_pubkey = tags[_TAG_PROVIDER]
    # 32-byte hex, so field boundaries in the signed binding can't be shifted.
    if not _is_hex(payment_hash, 32) or not _is_hex(provider_pubkey, 32):
        return None

    try:
        score = int(tags[_TAG_SCORE])
    except ValueError:
        return None
    if not 1 <= score <= 5:
        return None

    payer_pubkey = event.pubkey
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


def verify_attestations(events: Iterable[NostrEvent]) -> list[ReputationAttestation]:
    """Parse and cryptographically verify events into attestations — ONCE.

    This is the single verification boundary. Each event's Schnorr signature and
    provider binding are checked here (via parse_and_verify_rating); anything
    that fails is dropped. aggregate_reputation and compute_payer_trust take the
    result and never re-verify, so the same corpus isn't verified two or three
    times when both are computed over it. (Schnorr verification dominates the
    cost.) Pass the node's full cross-provider corpus once, then feed the parsed
    attestations to both consumers.
    """
    out: list[ReputationAttestation] = []
    for event in events:
        att = parse_and_verify_rating(event)
        if att is not None:
            out.append(att)
    return out


# ── Aggregation (dedupe → weighted score with sybil defenses) ───────────────
# Input is already-verified attestations from verify_attestations(); these
# functions do NOT re-check signatures.


@dataclass
class AggregateReputation:
    """A skill's federated trust view, aggregated from verified attestations."""

    skill_id: str
    score: float          # weighted mean over independent, deduped ratings
    distinct_payers: int  # distinct non-self rater keys
    total_ratings: int    # deduped independent ratings counted
    self_ratings: int     # excluded from the score (rater == provider)
    flags: list[str]


def compute_payer_trust(attestations: Iterable[ReputationAttestation]) -> dict[str, float]:
    """Web-of-trust weight per rater key, from cross-provider diversity.

    Takes already-verified attestations (see verify_attestations); it does not
    re-check signatures.

    Scope: pass the node's FULL cross-provider corpus; feeding a single skill's
    attestations collapses every rater to the floor weight (no signal).

    WEAK SIGNAL, not a cost anchor: provider keys and bindings are free and need
    no real payment (the phase-1 self-deal residual), so a sybil can farm
    cross-provider "diversity" with throwaway keys at near-zero cost. Treat this
    as a soft tie-breaker, never as proof of an independent rater; a real
    anti-sybil weight needs a cost anchor (real-payment proof / external identity).
    Weight ramps from 0.5 (one provider) to 1.0 (three or more).
    """
    providers_by_rater: dict[str, set[str]] = {}
    for att in attestations:
        providers_by_rater.setdefault(att.rater_pubkey, set()).add(att.provider_pubkey)
    return {
        rater: min(1.0, 0.5 + 0.25 * (len(provs) - 1))
        for rater, provs in providers_by_rater.items()
    }


def aggregate_reputation(
    *,
    skill_id: str,
    provider_pubkey: str,
    attestations: Iterable[ReputationAttestation],
    payer_trust: dict[str, float] | None = None,
) -> AggregateReputation:
    """Dedupe and aggregate already-verified attestations into a skill's trust view.

    Takes the output of verify_attestations(); it does NOT re-check signatures.

    Defenses against self-dealing / sybil. None eliminate it (a provider can be
    the payer); they raise cost and detectability:
      - only attestations for this skill AND this provider key count,
      - one rating per payment_hash. On a COLLISION (a payment_hash bound to more
        than one rater/score, which only a provider can mint) keep the LOWEST
        score and flag `duplicate_payment_binding`, so a provider cannot silently
        displace an honest low rating. `created_at` is attacker-set and is NOT
        used as a security tiebreak.
      - direct self-ratings (rater == provider) are excluded and flagged,
      - per-rater diminishing weight (1/n) times an optional web-of-trust weight,
      - rating concentration is flagged.
    """
    payer_trust = payer_trust or {}

    verified = [
        att
        for att in attestations
        if att.skill_id == skill_id and att.provider_pubkey == provider_pubkey
    ]

    # group by payment_hash; resolve collisions conservatively. created_at is
    # attacker-set, so it is NOT used to pick a winner.
    flags: list[str] = []
    groups: dict[str, list[ReputationAttestation]] = {}
    for att in verified:
        groups.setdefault(att.dedupe_key, []).append(att)
    deduped: list[ReputationAttestation] = []
    for group in groups.values():
        if len(group) == 1:
            deduped.append(group[0])
        elif len({a.rater_pubkey for a in group}) > 1 or len({a.score for a in group}) > 1:
            # Two different ratings for one payment: only a provider can mint this.
            # Keep the LOWEST score (so a provider can't displace an honest low
            # rating) and flag it instead of silently resolving.
            if "duplicate_payment_binding" not in flags:
                flags.append("duplicate_payment_binding")
            deduped.append(min(group, key=lambda a: a.score))
        else:
            deduped.append(group[0])  # benign re-broadcast of the same rating

    self_ratings = [a for a in deduped if a.rater_pubkey == provider_pubkey]
    independent = [a for a in deduped if a.rater_pubkey != provider_pubkey]

    if self_ratings:
        flags.append("self_ratings_present")
    if not independent:
        flags.append("no_independent_ratings")
        return AggregateReputation(skill_id, 0.0, 0, 0, len(self_ratings), flags)

    seen: dict[str, int] = {}
    weighted_sum = 0.0
    total_weight = 0.0
    # created_at only orders a rater's OWN repeat ratings for the 1/n weighting; it
    # is unauthenticated, but at most shuffles which of one rater's ratings gets
    # full weight (low stakes) and can never displace another rater.
    for att in sorted(independent, key=lambda a: a.created_at):
        seen[att.rater_pubkey] = seen.get(att.rater_pubkey, 0) + 1
        # payer_trust fails OPEN (unknown rater -> 1.0): web-of-trust weighting is
        # off unless the caller supplies it.
        weight = (1.0 / seen[att.rater_pubkey]) * payer_trust.get(att.rater_pubkey, 1.0)
        weighted_sum += att.score * weight
        total_weight += weight
    score = round(weighted_sum / total_weight, 2) if total_weight > 0 else 0.0

    counts = Counter(a.rater_pubkey for a in independent)
    if len(independent) >= 3 and max(counts.values()) / len(independent) >= 0.6:
        flags.append("rating_concentration")

    return AggregateReputation(
        skill_id=skill_id,
        score=score,
        distinct_payers=len(counts),
        total_ratings=len(independent),
        self_ratings=len(self_ratings),
        flags=flags,
    )


# ── Nostr transport (publish / fetch rating attestations) ───────────────────

# Default relays for federated reputation; callers may override.
DEFAULT_RATING_RELAYS: tuple[str, ...] = (
    "wss://relay.damus.io",
    "wss://nos.lol",
    "wss://relay.nostr.band",
)


def ratings_filter(provider_pubkey: str, *, since_hours: int = 0, limit: int = 500) -> dict:
    """REQ filter for a provider's rating attestations (kind 9070).

    Filters on the indexed single-letter `p` (provider) tag. The skill tag is
    multi-char and not relay-indexable, so narrowing to one skill happens client
    side in aggregate_reputation; fetch by provider, aggregate by skill.
    """
    since = int(time.time()) - since_hours * 3600 if since_hours > 0 else None
    return build_req_filter(
        kinds=[CONDUIT_RATING_KIND],
        tags={_TAG_PROVIDER: [provider_pubkey]},
        since=since,
        limit=limit,
    )


def dedupe_events(event_lists: Iterable[list[NostrEvent]]) -> list[NostrEvent]:
    """Merge events gathered from several relays, keeping one per event id."""
    by_id: dict[str, NostrEvent] = {}
    for events in event_lists:
        for event in events:
            by_id.setdefault(event.id, event)
    return list(by_id.values())


def _safe_relays(
    relay_urls: Iterable[str], *, allowlist: Sequence[str] = DEFAULT_RATING_RELAYS
) -> list[str]:
    """Drop relay URLs we refuse to open a websocket to (SSRF guard).

    URLs in the trusted allowlist (our hardcoded defaults) pass without a DNS
    round-trip. Any other URL — e.g. one sourced from an untrusted skill listing
    or peer once federation goes live — must pass validate_relay_url: wss:// only,
    host that does not resolve to a loopback/internal/metadata address. Unsafe
    URLs are dropped rather than raising, so one bad relay in a fan-out doesn't
    abort the others (mirrors the per-relay error tolerance of the gather).

    Blocking DNS happens here, so async callers offload it via asyncio.to_thread.
    """
    safe: list[str] = []
    for url in relay_urls:
        if url in allowlist:
            safe.append(url)
            continue
        try:
            validate_relay_url(url)
        except UnsafeURLError:
            continue
        safe.append(url)
    return safe


async def publish_rating(
    event: NostrEvent,
    relay_urls: Sequence[str] = DEFAULT_RATING_RELAYS,
    timeout: float = 10.0,
    *,
    validate_relays: bool = True,
) -> dict[str, bool]:
    """Publish a rating attestation to relays. Returns {relay_url: accepted}.

    Guards the kind so we never broadcast a non-rating event on this path. With
    validate_relays (the default), relay URLs outside the trusted default set are
    SSRF-checked and unsafe ones dropped before any connection; pass
    validate_relays=False only for trusted/synthetic URLs (e.g. tests).
    """
    import asyncio

    if event.kind != CONDUIT_RATING_KIND:
        raise ValueError(f"not a rating attestation (kind {event.kind})")
    urls = list(relay_urls)
    if validate_relays:
        urls = await asyncio.to_thread(_safe_relays, urls)
    return await publish_to_relays(event, urls, timeout=timeout)


async def fetch_ratings(
    provider_pubkey: str,
    relay_urls: Sequence[str] = DEFAULT_RATING_RELAYS,
    *,
    since_hours: int = 0,
    limit: int = 500,
    timeout: float = 10.0,
    validate_relays: bool = True,
) -> list[NostrEvent]:
    """Fetch a provider's rating attestation events (kind 9070) across relays.

    Concurrent; deduped by event id; each relay capped at `limit` events
    client-side, so a relay that ignores the filter `limit` can't make us buffer
    or Schnorr-verify more than that. With validate_relays (the default), relay
    URLs outside the trusted default set are SSRF-checked and unsafe ones dropped
    before any connection.

    subscribe() Schnorr-verifies each event for transport integrity, but the
    federation-level checks (binding, tags, hex) are NOT applied here. Verify once
    with verify_attestations(...), then feed the result to
    aggregate_reputation(skill_id=..., provider_pubkey=...) / compute_payer_trust.
    """
    import asyncio

    urls = list(relay_urls)
    if validate_relays:
        urls = await asyncio.to_thread(_safe_relays, urls)
    filt = ratings_filter(provider_pubkey, since_hours=since_hours, limit=limit)
    return await _subscribe_across_relays([filt], urls, timeout=timeout, max_events=limit)
