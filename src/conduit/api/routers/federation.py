"""Federation peering — serve this node's cached attestations to peers (Federation #2).

Public + read-only. The data is the same public reputation already broadcast to
Nostr relays, so peers fetch without a credential (rate-limited by the middleware).
Every served event is re-verified by the puller on ingest, so this endpoint exposes
nothing new and trusts no one.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.api.deps import get_session
from conduit.core.config import settings
from conduit.services.federation import is_pubkey_hex
from conduit.services.federation_cache import get_attestation_events

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
