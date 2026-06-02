"""Security & Verification endpoints — mirrors MCP security/verification tools.

7 endpoints:
  GET  /api/v1/security/spending
  POST /api/v1/security/macaroons
  GET  /api/v1/security/permissions
  GET  /api/v1/security/anomalies
  POST /api/v1/security/verification/request
  POST /api/v1/security/verification/submit
  GET  /api/v1/security/verification/{skill_id}
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.api.deps import get_lnd, get_session, verify_api_key
from conduit.services.anomaly_detector import get_anomaly_summary
from conduit.services.macaroon_auth import (
    PROFILES,
    derive_macaroon,
    get_active_permissions,
)
from conduit.services.provider_verification import (
    VerificationError,
    get_verification_status,
    start_domain_verification,
    start_node_verification,
    verify_domain,
    verify_node_signature,
)
from conduit.services.spending_limiter import get_spending_summary

router = APIRouter(
    prefix="/security",
    tags=["security"],
    dependencies=[Depends(verify_api_key)],
)


# ── Request models ────────────────────────────────────────────────────


class CreateMacaroonRequest(BaseModel):
    profile: str | None = Field(
        default=None, description="Profile name: admin, readonly, marketplace, spending"
    )
    permissions: list[str] | None = Field(default=None, description="Custom permission list")


class RequestVerificationRequest(BaseModel):
    skill_id: str = Field(..., description="Skill UUID to verify")
    method: str = Field(..., description="'node' or 'domain'")
    domain: str | None = Field(default=None, description="Required for domain verification")


class SubmitVerificationRequest(BaseModel):
    skill_id: str = Field(..., description="Skill UUID")
    method: str = Field(..., description="'node' or 'domain'")
    signature: str | None = Field(default=None, description="Node signature (for node method)")
    domain: str | None = Field(default=None, description="Domain to verify (for domain method)")


# ── Spending ──────────────────────────────────────────────────────────


@router.get("/spending")
async def spending_status():
    """Get current spending vs. configured limits."""
    return await get_spending_summary()


# ── Macaroons ─────────────────────────────────────────────────────────


@router.post("/macaroons")
async def create_macaroon(req: CreateMacaroonRequest):
    """Mint a scoped authorization token."""
    try:
        mac = derive_macaroon(profile=req.profile, permissions=req.permissions)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "macaroon": mac,
        "profile": req.profile,
        "available_profiles": list(PROFILES.keys()),
    }


@router.get("/permissions")
async def list_permissions():
    """Show active permissions for the current session."""
    active = get_active_permissions()
    return {
        "permissions": [p.value for p in active] if active else "unrestricted",
        "available_profiles": list(PROFILES.keys()),
    }


# ── Anomalies ─────────────────────────────────────────────────────────


@router.get("/anomalies")
async def anomaly_report():
    """View flagged suspicious transaction patterns."""
    return await get_anomaly_summary()


# ── Provider Verification ─────────────────────────────────────────────


@router.post("/verification/request")
async def request_verification(
    req: RequestVerificationRequest,
    session: AsyncSession = Depends(get_session),
):
    """Start node or domain verification — returns a challenge."""
    try:
        if req.method == "node":
            challenge = await start_node_verification(session, req.skill_id)
            return {
                "method": "node",
                "challenge": challenge,
                "instructions": (
                    "Sign this challenge with your node: "
                    f"lncli signmessage \"{challenge}\""
                ),
            }
        elif req.method == "domain":
            if not req.domain:
                raise HTTPException(
                    status_code=400, detail="Domain required for domain verification"
                )
            challenge = await start_domain_verification(session, req.skill_id, req.domain)
            return {
                "method": "domain",
                "challenge": challenge,
                "domain": req.domain,
                "instructions": (
                    f"Place this token at: "
                    f"https://{req.domain}/.well-known/conduit-verify.txt"
                ),
            }
        else:
            raise HTTPException(status_code=400, detail="Method must be 'node' or 'domain'")
    except VerificationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/verification/submit")
async def submit_verification(
    req: SubmitVerificationRequest,
    session: AsyncSession = Depends(get_session),
):
    """Complete verification with proof (signature or domain check)."""
    try:
        if req.method == "node":
            if not req.signature:
                raise HTTPException(
                    status_code=400, detail="Signature required for node verification"
                )
            result = await verify_node_signature(
                session, req.skill_id, req.signature, lnd=get_lnd(),
            )
            return result
        elif req.method == "domain":
            if not req.domain:
                raise HTTPException(
                    status_code=400, detail="Domain required for domain verification"
                )
            result = await verify_domain(session, req.skill_id, req.domain)
            return result
        else:
            raise HTTPException(status_code=400, detail="Method must be 'node' or 'domain'")
    except VerificationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/verification/{skill_id}")
async def verification_status(
    skill_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Check a skill's verification status and badges."""
    try:
        return await get_verification_status(session, skill_id)
    except VerificationError as e:
        raise HTTPException(status_code=404, detail=str(e))
