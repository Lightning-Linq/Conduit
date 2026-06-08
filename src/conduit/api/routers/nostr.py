"""Nostr endpoints — mirrors MCP Nostr tools over HTTP.

4 endpoints:
  POST /api/v1/nostr/publish
  GET  /api/v1/nostr/discover
  GET  /api/v1/nostr/profile
  GET  /api/v1/nostr/relays/status
"""


from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.api.deps import get_session, verify_api_key
from conduit.core.config import settings
from conduit.models.skill import Skill
from conduit.services.node_identity import get_node_keypair
from conduit.services.nostr import (
    NostrKeypair,
    NostrRelay,
    discover_from_relays,
    publish_to_relays,
    skill_to_event,
)

router = APIRouter(prefix="/nostr", tags=["nostr"])

def _get_nostr_keys() -> NostrKeypair:
    """The node's Nostr keypair (delegates to the shared node identity)."""
    return get_node_keypair()


def _get_relays(override: list[str] | None = None) -> list[str]:
    if override:
        return override
    return settings.nostr_relay_list


# --- Request/Response Models ---


class PublishRequest(BaseModel):
    skill_id: str = Field(..., description="Skill ID to publish to Nostr")
    relays: list[str] = Field(default=[], description="Override relay URLs")


class PublishResponse(BaseModel):
    event_id: str
    pubkey: str
    kind: int
    skill_name: str
    relay_results: dict[str, bool]


class NostrSkill(BaseModel):
    id: str = ""
    name: str = ""
    description: str = ""
    category: str = ""
    price_sats: int = 0
    provider_name: str = ""
    provider_lightning_address: str = ""
    nostr_event_id: str = ""
    nostr_pubkey: str = ""
    relay: str = ""


class DiscoverResponse(BaseModel):
    skills: list[NostrSkill]
    relays_searched: list[str]
    window_hours: int


class ProfileResponse(BaseModel):
    pubkey_hex: str
    npub: str
    key_source: str
    relays: list[str]
    local_skill_count: int


class RelayStatus(BaseModel):
    url: str
    status: str


class RelayStatusResponse(BaseModel):
    relays: list[RelayStatus]
    connected_count: int
    total_count: int


# --- Endpoints ---


@router.post("/publish", response_model=PublishResponse)
async def publish_skill(
    body: PublishRequest,
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(verify_api_key),
):
    """Publish a skill listing to Nostr relays."""
    # Find the skill
    from conduit.api.routers.marketplace import _get_skill_or_404
    skill = await _get_skill_or_404(session, body.skill_id)

    skill_dict = {
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
    }

    keys = _get_nostr_keys()
    event = skill_to_event(skill_dict, keys)

    relays = _get_relays(body.relays if body.relays else None)
    results = await publish_to_relays(event, relays)

    return PublishResponse(
        event_id=event.id,
        pubkey=keys.pubkey_hex,
        kind=event.kind,
        skill_name=skill.name,
        relay_results=results,
    )


@router.get("/discover", response_model=DiscoverResponse)
async def discover_skills(
    category: str = Query("", description="Filter by category"),
    max_price_sats: int = Query(0, description="Max price (0 = no limit)"),
    _key: str = Depends(verify_api_key),
):
    """Discover Conduit skills on Nostr relays."""
    relays = _get_relays()
    window = settings.nostr_discovery_window_hours

    skills = await discover_from_relays(
        relay_urls=relays,
        category=category,
        max_price_sats=max_price_sats,
        since_hours=window,
        limit=50,
    )

    return DiscoverResponse(
        skills=[
            NostrSkill(**{k: v for k, v in s.items() if k in NostrSkill.model_fields})
            for s in skills
        ],
        relays_searched=relays,
        window_hours=window,
    )


@router.get("/profile", response_model=ProfileResponse)
async def get_profile(
    session: AsyncSession = Depends(get_session),
    _key: str = Depends(verify_api_key),
):
    """Get Nostr identity for this Conduit node."""
    keys = _get_nostr_keys()

    skill_count = 0
    try:
        result = await session.execute(
            select(sa_func.count(Skill.id)).where(Skill.is_active.is_(True))
        )
        skill_count = result.scalar() or 0
    except Exception:
        pass

    return ProfileResponse(
        pubkey_hex=keys.pubkey_hex,
        npub=keys.npub,
        key_source="configured" if settings.nostr_private_key else "auto-generated",
        relays=settings.nostr_relay_list,
        local_skill_count=skill_count,
    )


@router.get("/relays/status", response_model=RelayStatusResponse)
async def relay_status(
    _key: str = Depends(verify_api_key),
):
    """Check connectivity to configured Nostr relays."""
    import asyncio
    relays = _get_relays()
    results: dict[str, str] = {}

    async def _check_one(url: str):
        try:
            async with NostrRelay(url, timeout=5.0):
                results[url] = "connected"
        except ImportError:
            results[url] = "websockets not installed"
        except Exception as e:
            results[url] = f"error: {type(e).__name__}"

    await asyncio.gather(*[_check_one(url) for url in relays])

    connected = sum(1 for v in results.values() if v == "connected")
    return RelayStatusResponse(
        relays=[RelayStatus(url=u, status=s) for u, s in results.items()],
        connected_count=connected,
        total_count=len(results),
    )
