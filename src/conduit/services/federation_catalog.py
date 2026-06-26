"""Federated skill-catalog cache — store/read verified remote skill listings.

Federation #2. Fetching listings over relays/peers is slow and re-verifies Schnorr;
this caches VERIFIED kind-38383 skill listings in Postgres so discovery can merge
remote skills locally. store_skill_events() is the trust boundary: it re-verifies
each event's signature, drops anything signed by THIS node (self-exclusion), parses
it to a skill, and upserts newest-wins on the NIP-33 (provider_pubkey, skill_id)
coordinate. get_cached_skills() reads them back for discovery.

Trust: a peer/relay is untrusted infrastructure. Re-verifying the signature means a
source cannot forge or inflate a listing — only serve junk (dropped here) or withhold
(mitigated by multiple sources). Provider verification badges are NOT trusted from
this cache; the federated reputation overlay (#1) is the cross-node trust signal.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.models.cached_skill import CachedSkill
from conduit.services.catalog_transport import NostrCatalogTransport, PeerCatalogTransport
from conduit.services.nostr import (
    NostrEvent,
    NostrKeypair,
    event_to_skill,
    skill_to_event,
)

# Columns overwritten when a strictly newer event replaces an existing coordinate.
# (provider_pubkey + skill_id are the conflict key and never change.)
_UPSERT_COLS = (
    "event_id", "event_created_at", "origin", "source_id", "provider_name",
    "provider_lightning_address", "name", "description", "category", "tags",
    "price_sats", "endpoint_url", "input_schema", "output_schema", "raw_event",
)

# Largest kind-38383 listing we ingest. A real listing is name + description +
# I/O JSON schemas + a few tags; even a rich one sits well under this, while a
# hostile public-relay event can be arbitrarily large and would bloat the cache
# (description/tags TEXT, raw_event JSONB). serialize_for_id() == the canonical
# [0,pubkey,created_at,kind,tags,content] payload — a close proxy for what's
# stored. Oversize events are DROPPED whole (never truncated: truncation would
# invalidate the signature and corrupt raw_event), and dropped BEFORE verify() so
# an attacker can't force unbounded hashing work.
_MAX_EVENT_BYTES = 32 * 1024


def _event_too_large(event: NostrEvent) -> bool:
    """True if the event's canonical payload exceeds the ingest size cap."""
    return len(event.serialize_for_id()) > _MAX_EVENT_BYTES


def _tags_from_event(event: NostrEvent) -> str | None:
    """Comma-joined 't' tag values (deduped, order-preserving) for the search column."""
    vals = [t[1] for t in event.tags if len(t) >= 2 and t[0] == "t" and t[1]]
    return ",".join(dict.fromkeys(vals)) or None


def _skill_row_values(
    event: NostrEvent, skill: dict, *, origin: str, source_id: str | None
) -> dict:
    """Column values for one verified skill-listing event."""
    return {
        "provider_pubkey": event.pubkey,  # the signer == the listing's provider
        "skill_id": skill["id"],
        "event_id": event.id,
        "event_created_at": event.created_at,
        "origin": origin,
        "source_id": source_id,
        "provider_name": skill.get("provider_name") or None,
        "provider_lightning_address": skill.get("provider_lightning_address") or None,
        "name": skill.get("name") or "",
        "description": skill.get("description") or None,
        "category": skill.get("category") or None,
        "tags": _tags_from_event(event),
        "price_sats": int(skill.get("price_sats") or 0),
        "endpoint_url": skill.get("endpoint_url") or None,
        "input_schema": skill.get("input_schema"),
        "output_schema": skill.get("output_schema"),
        "raw_event": event.to_dict(),
    }


def _skill_rows_from_events(
    events: Iterable[NostrEvent],
    *,
    self_pubkey: str,
    origin: str = "relay",
    source_id: str | None = None,
) -> list[dict]:
    """Verify, self-exclude, parse, and de-dup events into cache-row values.

    The trust boundary. For each event: it must be under the ingest size cap
    (oversize listings are dropped whole, before any hashing — DB-bloat guard);
    the Schnorr signature must verify; it must parse as a kind-38383 skill with a
    non-empty skill_id; and it must NOT be signed by this node (self-exclusion —
    never ingest our own catalog echoed back). Within the batch the newest
    event_created_at wins per (provider_pubkey, skill_id) so the upsert never tries
    to affect one coordinate twice.
    """
    newest: dict[tuple[str, str], dict] = {}
    for event in events:
        if _event_too_large(event):  # drop (don't truncate) before any hashing/parsing
            continue
        if not event.verify():  # re-verify signature on ingest
            continue
        if event.pubkey == self_pubkey:  # self-exclusion
            continue
        skill = event_to_skill(event)
        if skill is None or not skill.get("id"):
            continue
        row = _skill_row_values(event, skill, origin=origin, source_id=source_id)
        key = (row["provider_pubkey"], row["skill_id"])
        current = newest.get(key)
        if current is None or row["event_created_at"] > current["event_created_at"]:
            newest[key] = row
    return list(newest.values())


async def store_skill_events(
    session: AsyncSession,
    events: Iterable[NostrEvent],
    *,
    self_pubkey: str | None = None,
    origin: str = "relay",
    source_id: str | None = None,
) -> int:
    """Verify + upsert remote skill listings (newest-wins). Returns rows written.

    NIP-33 replaceable: ON CONFLICT on the (provider_pubkey, skill_id) coordinate
    updates only when the incoming event_created_at is strictly newer, so a re-fetched
    or stale listing is a no-op. self_pubkey defaults to this node's Nostr key (for
    self-exclusion); pass it explicitly to keep callers pure. The caller commits.
    """
    if self_pubkey is None:
        from conduit.services.node_identity import get_node_keypair

        self_pubkey = get_node_keypair().pubkey_hex
    rows = _skill_rows_from_events(
        events, self_pubkey=self_pubkey, origin=origin, source_id=source_id
    )
    if not rows:
        return 0
    stmt = pg_insert(CachedSkill).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["provider_pubkey", "skill_id"],
        set_={col: getattr(stmt.excluded, col) for col in _UPSERT_COLS},
        where=CachedSkill.event_created_at < stmt.excluded.event_created_at,
    )
    await session.execute(stmt)
    return len(rows)


async def get_cached_skills(
    session: AsyncSession,
    *,
    category: str | None = None,
    search: str | None = None,
    provider_pubkey: str | None = None,
    limit: int = 100,
) -> list[CachedSkill]:
    """Read cached remote skill listings for discovery (newest first)."""
    stmt = select(CachedSkill)
    if category:
        stmt = stmt.where(CachedSkill.category == category)
    if provider_pubkey:
        stmt = stmt.where(CachedSkill.provider_pubkey == provider_pubkey)
    if search:
        like = f"%{search}%"
        stmt = stmt.where(
            or_(CachedSkill.name.ilike(like), CachedSkill.description.ilike(like))
        )
    stmt = stmt.order_by(CachedSkill.event_created_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return list(result.scalars().all())


# --- Serve side: this node's own catalog as signed events (Task 4) ---


def _local_skill_to_event(skill, keypair: NostrKeypair) -> NostrEvent:
    """Build a signed kind-38383 listing event for one of this node's local skills."""
    return skill_to_event(
        {
            "id": str(skill.id),
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "tags": skill.tags or "",
            "price_sats": skill.price_sats,
            "provider_name": skill.provider_name,
            "provider_lightning_address": skill.provider_lightning_address or "",
            "input_schema": skill.input_schema,
            "output_schema": skill.output_schema,
            "endpoint_url": skill.endpoint_url or "",
        },
        keypair,
    )


async def get_local_skill_events(
    session: AsyncSession,
    *,
    since: int = 0,
    limit: int = 500,
    keypair: NostrKeypair | None = None,
) -> list[dict]:
    """This node's active skills as freshly signed kind-38383 events (peer serve payload).

    Signed with the node's Nostr key; a puller re-verifies on ingest, so this exposes
    only already-publishable listing data. ``since`` filters by skill updated_at for
    incremental pulls; results are newest-first, capped at ``limit``.
    """
    from conduit.models.skill import Skill

    if keypair is None:
        from conduit.services.node_identity import get_node_keypair

        keypair = get_node_keypair()
    stmt = select(Skill).where(Skill.is_active.is_(True))
    if since > 0:
        stmt = stmt.where(Skill.updated_at >= datetime.fromtimestamp(since, tz=UTC))
    stmt = stmt.order_by(Skill.updated_at.desc()).limit(limit)
    result = await session.execute(stmt)
    return [_local_skill_to_event(s, keypair).to_dict() for s in result.scalars().all()]


# --- Refresh: pull both transports into the cache (Task 5) ---


async def refresh_catalog(
    session: AsyncSession,
    *,
    relay_urls: Sequence[str] = (),
    peer_urls: Sequence[str] = (),
    since: int = 0,
    limit: int = 500,
    self_pubkey: str | None = None,
) -> int:
    """Pull skill listings from relays AND peers and cache the verified ones.

    Each transport is fetched independently and stored with its origin tag; every
    event is re-verified + self-excluded on store (store_skill_events). Returns the
    total listings written. The caller commits the session.
    """
    relay_events = await NostrCatalogTransport(relay_urls).fetch_skills(since=since, limit=limit)
    total = await store_skill_events(
        session, relay_events, self_pubkey=self_pubkey, origin="relay"
    )
    peer_events = await PeerCatalogTransport(peer_urls).fetch_skills(since=since, limit=limit)
    total += await store_skill_events(
        session, peer_events, self_pubkey=self_pubkey, origin="peer"
    )
    return total


# --- Discovery merge: fold cached remote skills into local results (Task 6) ---


def merge_discovery(
    local_skills,
    cached_skills,
    *,
    node_pubkey: str,
    max_price: int = 0,
) -> list[dict]:
    """Merge local + cached remote skills into one discovery list.

    Dedup by (provider_pubkey, skill_id) preferring local; tag each result's origin
    (local|peer|relay) and signer pubkey. Remote verification badges are NEUTRALIZED —
    a peer is untrusted to assert verification, so only this node's own skills carry a
    real verification_status. The federated reputation overlay can still be applied
    downstream via each result's provider_pubkey.
    """
    items: list[dict] = []
    local_coords: set[tuple[str, str]] = set()
    for s in local_skills:
        sid = str(s.id)
        local_coords.add((node_pubkey, sid))
        items.append(
            {
                "id": sid,
                "name": s.name,
                "description": s.description,
                "provider": s.provider_name,
                "category": s.category,
                "price_sats": s.price_sats,
                "verification_status": s.verification_status,
                "origin": "local",
                "provider_pubkey": node_pubkey,
            }
        )
    for c in cached_skills:
        if max_price and c.price_sats > max_price:
            continue
        if (c.provider_pubkey, c.skill_id) in local_coords:
            continue  # prefer local on a coordinate clash
        items.append(
            {
                "id": c.skill_id,
                "name": c.name,
                "description": c.description,
                "provider": c.provider_name,
                "category": c.category,
                "price_sats": c.price_sats,
                "verification_status": "unverified",  # neutralized — peer badges not trusted
                "origin": c.origin,
                "provider_pubkey": c.provider_pubkey,
            }
        )
    return items


async def apply_reputation_overlay(session, items: list[dict]) -> None:
    """Attach federated_reputation (dict or None) to each discovery item, in place.

    Keyed by each item's (id, provider_pubkey) — so it works for both local and remote
    skills. One indexed read per item (use_web_of_trust=False); fail-soft per item, so a
    read miss/error leaves federated_reputation = None and never breaks discovery.
    """
    from conduit.services.federation_cache import get_cached_reputation

    for item in items:
        item["federated_reputation"] = None
        try:
            agg = await get_cached_reputation(
                session,
                skill_id=item["id"],
                provider_pubkey=item["provider_pubkey"],
                use_web_of_trust=False,
            )
            if agg.total_ratings > 0:
                item["federated_reputation"] = {
                    "score": agg.score,
                    "distinct_payers": agg.distinct_payers,
                    "total_ratings": agg.total_ratings,
                    "flags": agg.flags,
                }
        except Exception:
            pass  # fail-soft: the reputation overlay must never break discovery


async def is_cached_skill(session, skill_id: str) -> bool:
    """True if skill_id is a known remote (cached) skill — i.e. not hosted locally.

    The execution guard uses this: cross-node execution is Federation #3, so a request
    to run a cached remote skill is rejected with a clear error rather than a bare 404.
    """
    result = await session.execute(
        select(CachedSkill.skill_id).where(CachedSkill.skill_id == skill_id).limit(1)
    )
    return result.scalar_one_or_none() is not None
