"""Federation peering — serve this node's catalog, reputation, and (opt-in) executions.

The attestation + skill endpoints (Federation #1.5 / #2) are public and read-only:
the data is the same public reputation and listings already broadcast to Nostr
relays, so peers fetch without a credential (rate-limited by the middleware). Every
served event is re-verified by the puller on ingest, so they expose nothing new and
trust no one.

The execution endpoints (Federation #3) are WRITE endpoints and also unauthenticated
— that is what makes cross-node buying open — so they are gated behind
FEDERATION_EXECUTION_ENABLED, off by default. They deliberately contain no money
logic of their own: each delegates to the marketplace handler that already serves
local buyers, so a cross-node purchase and a local one cannot drift apart. On top of
that, the request endpoint refuses skills this node merely has cached, so it never
brokers onward — that is what prevents A -> B -> C chaining and amplification.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.api.deps import get_session, verify_api_key
from conduit.api.routers.marketplace import (
    ConfirmExecutionRequest,
    RequestExecutionRequest,
    _resolve_local_skill_or_remote,
    confirm_skill_execution,
    request_skill_execution,
)
from conduit.core.config import settings
from conduit.core.verification_policy import is_verified_status
from conduit.services.federation import is_pubkey_hex
from conduit.services.federation_cache import get_attestation_events, refresh_all_cached
from conduit.services.federation_catalog import get_local_skill_events, refresh_catalog

router = APIRouter(prefix="/federation", tags=["federation"])


def _require_cross_node_execution() -> None:
    """Gate the Federation #3 write endpoints.

    Two distinct refusals on purpose: federation off means 'not here at all' (404,
    matching the read endpoints above), while federation on but cross-node execution
    off means 'this node federates discovery only' (501), which is the same milestone
    message a buyer already gets from the marketplace router.
    """
    if not settings.federation_enabled:
        raise HTTPException(status_code=404, detail="Federation is disabled on this node")
    if not settings.federation_execution_enabled:
        raise HTTPException(
            status_code=501,
            detail=(
                "Cross-node execution (Federation #3) is not enabled on this node. "
                "The operator federates discovery only; set FEDERATION_EXECUTION_ENABLED "
                "to accept executions from peers."
            ),
        )


def _require_verified_skill(skill) -> None:
    """Apply REQUIRE_VERIFIED_SKILLS to cross-node buyers too.

    VerificationEnforcementMiddleware only matches the exact path
    /api/v1/marketplace/executions, so it does not see this route. Without this
    check an operator who blocks unverified skills would still sell them to any
    peer — the policy has to hold for every buyer, not just local ones.

    The predicate is shared (core/verification_policy.py) so this door, the
    middleware, and mcp_server cannot answer "is it verified?" differently.
    """
    if not settings.require_verified_skills:
        return
    status = getattr(skill, "verification_status", None)
    if not is_verified_status(status):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "skill_not_verified",
                "detail": (
                    f"Skill is '{status}'. This node blocks execution of unverified "
                    "skills by policy, for local and cross-node buyers alike."
                ),
                "verification_status": status,
            },
        )


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


@router.post("/executions")
async def serve_execution_request(
    req: RequestExecutionRequest,
    session: AsyncSession = Depends(get_session),
):
    """Sell one of THIS node's skills to a peer's agent (Federation #3).

    Unauthenticated by design: an open marketplace lets any node buy from any node,
    and the thing being handed back is a Lightning invoice, which is public by
    nature. The buyer pays it directly, so this node takes no custody and the caller
    gains nothing without paying.

    No money logic here — request_skill_execution mints the invoices and enforces
    the listing's active state.

    THIS NODE DOES NOT BROKER ONWARD. A skill we merely have cached from another
    peer is refused, so a caller cannot chain A -> B -> C: that would let one
    request fan out across the federation (amplification), obscure who is actually
    being paid, and create cycles between two nodes that each cache the other.
    Selling is a local-only act; buying elsewhere is the caller's own business.
    """
    _require_cross_node_execution()
    skill = await _resolve_local_skill_or_remote(session, req.skill_id)
    if skill is None:
        raise HTTPException(
            status_code=501,
            detail=(
                "This node does not broker cross-node executions onward (Federation "
                "#3). It sells only the skills it hosts locally; buy that skill from "
                "the node that hosts it."
            ),
        )
    _require_verified_skill(skill)
    return await request_skill_execution(req, session)


@router.post("/executions/{execution_id}/confirm")
async def serve_execution_confirm(
    execution_id: str,
    req: ConfirmExecutionRequest,
    session: AsyncSession = Depends(get_session),
):
    """Confirm a peer-brokered purchase and deliver the result (Federation #3).

    Unauthenticated like the request endpoint, and safe for the same reason: the
    caller must present a preimage that SHA256s to the execution's payment hash,
    and the marketplace handler independently verifies settlement with this node's
    wallet. Knowing an execution id proves nothing on its own.

    No _require_verified_skill here, deliberately: the buyer has already paid by
    this point, so refusing delivery would keep a settled payment and hand back
    nothing. REQUIRE_VERIFIED_SKILLS is a request-time gate at every door.
    """
    _require_cross_node_execution()
    return await confirm_skill_execution(execution_id, req, session)


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
