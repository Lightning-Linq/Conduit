"""Federation peering — serve this node's cached attestations to peers (Federation #2).

Public + read-only. The data is the same public reputation already broadcast to
Nostr relays, so peers fetch without a credential (rate-limited by the middleware).
Every served event is re-verified by the puller on ingest, so this endpoint exposes
nothing new and trusts no one.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.api.deps import get_session, verify_api_key
from conduit.core.config import settings
from conduit.services.federation import is_pubkey_hex
from conduit.services.federation_cache import get_attestation_events, refresh_all_cached
from conduit.services.federation_catalog import get_local_skill_events, refresh_catalog

router = APIRouter(prefix="/federation", tags=["federation"])


@router.get("/attestations")
async def serve_attestations(
    provider_pubkey: str = Query(..., description="Provider Nostr x-only pubkey (64 hex)"),
    since: int = Query(0, ge=0, description="Only events with created_at >= since (unix)"),
    limit: int = Query(500, ge=1, le=1000, description="Max events to return"),
    session: AsyncSession = Depends(get_session),
):
    """This node's cached kind-9070 attestation events for a provider.

    Public read endpoint (peers re-verify on ingest). 404 when federation is off.
    """
    if not settings.federation_enabled:
        raise HTTPException(status_code=404, detail="Federation is disabled on this node")
    if not is_pubkey_hex(provider_pubkey):
        raise HTTPException(status_code=422, detail="provider_pubkey must be 64 hex chars")
    events = await get_attestation_events(
        session, provider_pubkey=provider_pubkey, since=since, limit=limit
    )
    return {"attestations": events, "count": len(events)}


@router.get("/skills")
async def serve_skills(
    since: int = Query(0, ge=0, description="Only skills updated >= since (unix)"),
    limit: int = Query(500, ge=1, le=500, description="Max skills to return"),
    session: AsyncSession = Depends(get_session),
):
    """This node's active skills as signed kind-38383 listing events.

    Public read endpoint (peers re-verify on ingest). 404 when federation is off.
    """
    if not settings.federation_enabled:
        raise HTTPException(status_code=404, detail="Federation is disabled on this node")
    events = await get_local_skill_events(session, since=since, limit=limit)
    return {"skills": events, "count": len(events)}


@router.post("/refresh", dependencies=[Depends(verify_api_key)])
async def trigger_refresh(session: AsyncSession = Depends(get_session)):
    """Manually pull relays + peers into the cache for known providers.

    Admin action (API key required), unlike the public serve endpoint. The
    background loop does this on a timer; this is for on-demand / MCP-only nodes.
    """
    if not settings.federation_enabled:
        raise HTTPException(status_code=404, detail="Federation is disabled on this node")
    n = await refresh_all_cached(
        session,
        relay_urls=settings.nostr_relay_list,
        peer_urls=settings.federation_peer_list,
    )
    skills = await refresh_catalog(
        session,
        relay_urls=settings.nostr_relay_list,
        peer_urls=settings.federation_peer_list,
    )
    await session.commit()
    return {"refreshed": n, "skills_cached": skills}
