"""Marketplace endpoints — mirrors MCP marketplace tools over HTTP.

6 endpoints:
  GET  /api/v1/marketplace/skills
  GET  /api/v1/marketplace/skills/{skill_id}
  POST /api/v1/marketplace/skills
  POST /api/v1/marketplace/executions
  POST /api/v1/marketplace/executions/{execution_id}/confirm
  POST /api/v1/marketplace/executions/{execution_id}/rate
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.api.deps import verify_api_key, get_lnd, get_session
from conduit.models.skill import Skill
from conduit.models.execution import SkillExecution, ExecutionStatus
from conduit.models.rating import Rating
from conduit.services.rating_integrity import (
    validate_rating,
    calculate_weighted_rating,
    check_provider_rating_concentration,
    RatingIntegrityError,
)
from conduit.services.rate_limiter import rate_limiter, RateLimitExceeded
from conduit.services.url_safety import UnsafeURLError, validate_outbound_url

router = APIRouter(
    prefix="/marketplace",
    tags=["marketplace"],
    dependencies=[Depends(verify_api_key)],
)


# ── Request / Response models ─────────────────────────────────────────


class RegisterSkillRequest(BaseModel):
    name: str = Field(..., description="Skill name")
    description: str = Field(..., description="What this skill does")
    provider_name: str = Field(..., description="Provider/publisher name")
    category: str = Field(default="general", description="Skill category")
    price_sats: int = Field(default=0, ge=0, description="Price in satoshis")
    lightning_address: str = Field(default="", description="Provider's Lightning address")
    input_schema: dict | None = Field(default=None, description="JSON schema for inputs")
    output_schema: dict | None = Field(default=None, description="JSON schema for outputs")
    webhook_url: str | None = Field(default=None, description="Execution webhook URL")


class RequestExecutionRequest(BaseModel):
    skill_id: str = Field(..., description="UUID of the skill to execute")
    consumer_name: str = Field(default="anonymous", description="Who is buying")
    input_data: dict | None = Field(default=None, description="Input payload")


class ConfirmExecutionRequest(BaseModel):
    payment_hash: str = Field(..., description="Payment hash proving payment")


class SubmitRatingRequest(BaseModel):
    score: int = Field(..., ge=1, le=5, description="Rating 1-5")
    review: str = Field(default="", description="Optional review text")
    payment_preimage: str = Field(..., description="Preimage proving payment (hex)")


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("/skills")
async def discover_skills(
    keyword: str = Query(default="", description="Search keyword"),
    category: str = Query(default="", description="Filter by category"),
    max_price: int = Query(default=0, ge=0, description="Max price in sats (0=no limit)"),
    session: AsyncSession = Depends(get_session),
):
    """Discover skills by keyword, category, or price range."""
    try:
        rate_limiter.check("discover_skills")
    except RateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))

    query = select(Skill)
    if keyword:
        query = query.where(
            or_(
                Skill.name.ilike(f"%{keyword}%"),
                Skill.description.ilike(f"%{keyword}%"),
            )
        )
    if category:
        query = query.where(Skill.category == category)
    if max_price > 0:
        query = query.where(Skill.price_sats <= max_price)

    result = await session.execute(query.limit(50))
    skills = result.scalars().all()

    return {
        "count": len(skills),
        "skills": [
            {
                "id": str(s.id),
                "name": s.name,
                "description": s.description,
                "provider": s.provider_name,
                "category": s.category,
                "price_sats": s.price_sats,
                "verification_status": s.verification_status,
            }
            for s in skills
        ],
    }


@router.get("/skills/{skill_id}")
async def get_skill_details(
    skill_id: str,
    session: AsyncSession = Depends(get_session),
):
    """Get full details for a skill including ratings."""
    try:
        rate_limiter.check("get_skill_details")
    except RateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))

    skill = await _get_skill_or_404(session, skill_id)
    weighted_rating = await calculate_weighted_rating(session, skill.id)

    return {
        "id": str(skill.id),
        "name": skill.name,
        "description": skill.description,
        "provider": skill.provider_name,
        "category": skill.category,
        "price_sats": skill.price_sats,
        "lightning_address": skill.provider_lightning_address,
        "input_schema": skill.input_schema,
        "output_schema": skill.output_schema,
        "webhook_url": skill.endpoint_url,
        "verification_status": skill.verification_status,
        "verified_node_pubkey": skill.verified_node_pubkey,
        "verified_domain": skill.verified_domain,
        "weighted_rating": weighted_rating,
    }


@router.post("/skills", status_code=201)
async def register_skill(
    req: RegisterSkillRequest,
    session: AsyncSession = Depends(get_session),
):
    """Register a new skill on the marketplace."""
    try:
        rate_limiter.check("register_skill")
    except RateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))

    # If a webhook is provided, validate it now. Conduit will POST the
    # payment preimage to this URL on every execution; we refuse to even
    # store a URL that points at internal services.
    if req.webhook_url:
        try:
            validate_outbound_url(req.webhook_url)
        except UnsafeURLError as e:
            raise HTTPException(
                status_code=400,
                detail=f"webhook_url rejected: {e}",
            )

    skill = Skill(
        name=req.name,
        description=req.description,
        provider_name=req.provider_name,
        category=req.category,
        price_sats=req.price_sats,
        provider_lightning_address=req.lightning_address,
        input_schema=req.input_schema,
        output_schema=req.output_schema,
        endpoint_url=req.webhook_url,
    )
    session.add(skill)
    await session.commit()

    return {
        "id": str(skill.id),
        "name": skill.name,
        "provider": skill.provider_name,
        "price_sats": skill.price_sats,
    }


@router.post("/executions")
async def request_skill_execution(
    req: RequestExecutionRequest,
    session: AsyncSession = Depends(get_session),
):
    """Request execution of a skill — generates an invoice for payment."""
    try:
        rate_limiter.check("request_skill_execution")
    except RateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))

    skill = await _get_skill_or_404(session, req.skill_id)

    # Create invoice if skill has a price
    payment_request = None
    payment_hash = None
    if skill.price_sats > 0:
        lnd = get_lnd()
        invoice = lnd.create_invoice(
            amount_msats=skill.price_sats * 1000,
            memo=f"Conduit skill: {skill.name}",
        )
        payment_request = invoice.payment_request
        payment_hash = invoice.payment_hash

    execution = SkillExecution(
        skill_id=skill.id,
        consumer_name=req.consumer_name,
        input_data=req.input_data,
        payment_hash=payment_hash,
        status=ExecutionStatus.PENDING_PAYMENT if skill.price_sats > 0 else ExecutionStatus.PENDING,
    )
    session.add(execution)
    await session.commit()

    return {
        "execution_id": str(execution.id),
        "skill_name": skill.name,
        "price_sats": skill.price_sats,
        "payment_request": payment_request,
        "payment_hash": payment_hash,
        "status": execution.status.value,
    }


@router.post("/executions/{execution_id}/confirm")
async def confirm_skill_execution(
    execution_id: str,
    req: ConfirmExecutionRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Confirm payment for an execution and trigger the webhook.

    The provided payment_hash must match this execution AND the invoice
    must be settled on the Lightning node — payment_hashes are returned
    to the buyer at request time, so without the settlement check anyone
    could mark an execution COMPLETED without paying.
    """
    try:
        rate_limiter.check("confirm_skill_execution")
    except RateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))

    try:
        exec_uuid = uuid.UUID(execution_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid execution ID")

    result = await session.execute(
        select(SkillExecution).where(SkillExecution.id == exec_uuid)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    if execution.status != ExecutionStatus.PENDING_PAYMENT:
        raise HTTPException(
            status_code=409,
            detail=f"Execution is not awaiting payment (status: {execution.status.value})",
        )

    if not execution.payment_hash or execution.payment_hash != req.payment_hash:
        raise HTTPException(status_code=400, detail="Payment hash does not match execution")

    # Talk to LND to verify the invoice is actually settled. Without this,
    # the caller is self-attesting that they paid.
    lnd = get_lnd()
    try:
        invoice_status = lnd.lookup_invoice(execution.payment_hash)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Could not check invoice: {e}")

    if not invoice_status.get("settled"):
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment_not_settled",
                "payment_hash": execution.payment_hash,
                "message": "Pay the invoice on Lightning first, then retry confirm.",
            },
        )

    execution.status = ExecutionStatus.COMPLETED
    execution.updated_at = datetime.now(timezone.utc)
    await session.commit()

    return {
        "execution_id": str(execution.id),
        "status": execution.status.value,
        "message": "Execution confirmed. Skill delivery in progress.",
    }


@router.post("/executions/{execution_id}/rate")
async def submit_rating(
    execution_id: str,
    req: SubmitRatingRequest,
    session: AsyncSession = Depends(get_session),
):
    """Rate a completed skill execution (requires payment preimage proof)."""
    try:
        rate_limiter.check("submit_rating")
    except RateLimitExceeded as e:
        raise HTTPException(status_code=429, detail=str(e))

    try:
        exec_uuid = uuid.UUID(execution_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid execution ID")

    result = await session.execute(
        select(SkillExecution).where(SkillExecution.id == exec_uuid)
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise HTTPException(status_code=404, detail="Execution not found")

    # Get the skill
    skill_result = await session.execute(
        select(Skill).where(Skill.id == execution.skill_id)
    )
    skill = skill_result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    # Validate rating integrity
    try:
        await validate_rating(session, execution, req.payment_preimage, skill)
    except RatingIntegrityError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Create rating
    rating = Rating(
        execution_id=execution.id,
        score=req.score,
        review=req.review,
        payment_preimage=req.payment_preimage,
    )
    session.add(rating)

    # Check for rating concentration
    flag = await check_provider_rating_concentration(
        session, skill, execution.consumer_name,
    )
    if flag:
        session.add(flag)

    await session.commit()

    weighted = await calculate_weighted_rating(session, skill.id)

    return {
        "rating_id": str(rating.id),
        "score": req.score,
        "weighted_average": weighted,
        "execution_id": execution_id,
    }


# ── Helpers ───────────────────────────────────────────────────────────


async def _get_skill_or_404(session: AsyncSession, skill_id: str) -> Skill:
    """Fetch a skill by UUID or raise 404."""
    try:
        uid = uuid.UUID(skill_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid skill ID format")

    result = await session.execute(select(Skill).where(Skill.id == uid))
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    return skill
