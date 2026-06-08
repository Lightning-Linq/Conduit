"""Federated reputation cache — store/read verified attestations in Postgres.

Federation #1, phase 4. fetch_ratings() (Nostr) is slow and re-verifies Schnorr;
this caches verified attestations so discovery aggregates locally. store_events()
is the trust boundary (it re-verifies before writing); get_cached_reputation()
reads rows back and runs the same aggregation a live fetch would.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.models.federated_attestation import FederatedAttestation
from conduit.services.federation import (
    DEFAULT_RATING_RELAYS,
    AggregateReputation,
    ReputationAttestation,
    aggregate_reputation,
    attestation_matches_execution,
    compute_payer_trust,
    fetch_ratings,
    parse_and_verify_rating,
    publish_rating,
)
from conduit.services.nostr import NostrEvent


def _row_to_attestation(row: FederatedAttestation) -> ReputationAttestation:
    """A cache row as the in-memory attestation the aggregator consumes."""
    return ReputationAttestation(
        skill_id=row.skill_id,
        provider_pubkey=row.provider_pubkey,
        rater_pubkey=row.rater_pubkey,
        payment_hash=row.payment_hash,
        score=row.score,
        created_at=row.attestation_created_at,
    )


def _row_values(event: NostrEvent, att: ReputationAttestation) -> dict:
    """Column values for one verified attestation event."""
    return {
        "event_id": event.id,
        "skill_id": att.skill_id,
        "provider_pubkey": att.provider_pubkey,
        "rater_pubkey": att.rater_pubkey,
        "payment_hash": att.payment_hash,
        "score": att.score,
        "attestation_created_at": att.created_at,
        "raw_event": event.to_dict(),
    }


def _rows_from_events(events: Iterable[NostrEvent]) -> list[dict]:
    """Verify events and return cache-row values for the valid ones.

    The cache's trust boundary: parse_and_verify_rating runs here and invalid
    events are dropped, so nothing unverified is ever written to the table.
    """
    return [
        _row_values(event, att)
        for event in events
        if (att := parse_and_verify_rating(event)) is not None
    ]


async def store_events(session: AsyncSession, events: Iterable[NostrEvent]) -> int:
    """Verify events and upsert the valid ones. Returns how many were written.

    Idempotent: a re-fetched event (same event_id) hits ON CONFLICT DO NOTHING.
    The caller commits the session.
    """
    rows = _rows_from_events(events)
    if not rows:
        return 0
    stmt = (
        pg_insert(FederatedAttestation)
        .values(rows)
        .on_conflict_do_nothing(index_elements=["event_id"])
    )
    await session.execute(stmt)
    return len(rows)


async def _load(
    session: AsyncSession,
    *,
    skill_id: str | None = None,
    provider_pubkey: str | None = None,
) -> list[ReputationAttestation]:
    """Load cached attestations, optionally narrowed to a skill and/or provider."""
    stmt = select(FederatedAttestation)
    if skill_id is not None:
        stmt = stmt.where(FederatedAttestation.skill_id == skill_id)
    if provider_pubkey is not None:
        stmt = stmt.where(FederatedAttestation.provider_pubkey == provider_pubkey)
    result = await session.execute(stmt)
    return [_row_to_attestation(row) for row in result.scalars().all()]


async def get_cached_reputation(
    session: AsyncSession,
    *,
    skill_id: str,
    provider_pubkey: str,
    use_web_of_trust: bool = True,
) -> AggregateReputation:
    """Aggregate a skill's cached attestations into its trust view.

    Reads the (skill, provider) rows and runs the standard aggregation. With
    use_web_of_trust, payer-trust weights are derived from the FULL cached corpus
    (cross-provider), matching compute_payer_trust's documented scope; that is a
    full-table read, so a hot path may pass use_web_of_trust=False.
    """
    if use_web_of_trust:
        # WoT needs the full cross-provider corpus anyway; load it once and let
        # aggregate_reputation filter the skill+provider slice (no second query).
        corpus = await _load(session)
        return aggregate_reputation(
            skill_id=skill_id,
            provider_pubkey=provider_pubkey,
            attestations=corpus,
            payer_trust=compute_payer_trust(corpus),
        )
    attestations = await _load(session, skill_id=skill_id, provider_pubkey=provider_pubkey)
    return aggregate_reputation(
        skill_id=skill_id,
        provider_pubkey=provider_pubkey,
        attestations=attestations,
    )


async def refresh_provider(
    session: AsyncSession,
    provider_pubkey: str,
    relay_urls: Sequence[str] = DEFAULT_RATING_RELAYS,
    **fetch_kwargs,
) -> int:
    """Fetch a provider's attestations from relays and cache the valid ones.

    Returns the number stored. The caller commits the session.
    """
    events = await fetch_ratings(provider_pubkey, relay_urls, **fetch_kwargs)
    return await store_events(session, events)


async def submit_attestation(
    session: AsyncSession,
    event: NostrEvent,
    *,
    skill_id: str,
    provider_pubkey: str,
    payment_hash: str,
    payer_pubkey: str,
    relay_urls: Sequence[str] = DEFAULT_RATING_RELAYS,
    validate_relays: bool = True,
) -> dict | None:
    """Verify a rating attestation belongs to this execution, then publish + cache.

    Returns {"event_id", "relays"} on success, or None if the event fails
    verification or does not match the execution (no publish, no cache). The match
    check is the anti-laundering guard: the event must be for this skill/provider/
    payment and signed by the captured payer key.
    """
    att = parse_and_verify_rating(event)
    if att is None or not attestation_matches_execution(
        att,
        skill_id=skill_id,
        provider_pubkey=provider_pubkey,
        payment_hash=payment_hash,
        payer_pubkey=payer_pubkey,
    ):
        return None
    relay_results = await publish_rating(event, relay_urls, validate_relays=validate_relays)
    await store_events(session, [event])
    return {"event_id": event.id, "relays": relay_results}
