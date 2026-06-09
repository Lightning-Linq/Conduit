"""Marketplace endpoints — mirrors MCP marketplace tools over HTTP.

8 endpoints:
  GET    /api/v1/marketplace/skills
  GET    /api/v1/marketplace/skills/{skill_id}
  POST   /api/v1/marketplace/skills
  DELETE /api/v1/marketplace/skills/{skill_id}
  POST   /api/v1/marketplace/executions
  DELETE /api/v1/marketplace/executions/{execution_id}
  POST   /api/v1/marketplace/executions/{execution_id}/confirm
  POST   /api/v1/marketplace/executions/{execution_id}/rate
"""

import hashlib
import sys
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.api.deps import get_lnd, get_session, verify_api_key
from conduit.core.config import settings
from conduit.models.execution import ExecutionStatus, SkillExecution
from conduit.models.rating import Rating
from conduit.models.skill import Skill
from conduit.services.anomaly_detector import check_for_anomalies
from conduit.services.federation import (
    build_rating_attestation,
    is_pubkey_hex,
    mint_execution_binding,
    publish_rating,
)
from conduit.services.federation_cache import get_cached_reputation, submit_attestation
from conduit.services.fee_calculator import calculate_fee
from conduit.services.node_identity import get_node_keypair
from conduit.services.nostr import NostrEvent
from conduit.services.rating_integrity import (
    RatingIntegrityError,
    calculate_weighted_rating,
    check_provider_rating_concentration,
    validate_rating,
)
from conduit.services.skill_executor import SkillExecutionError, execute_skill_webhook
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
    payer_pubkey: str | None = Field(
        default=None,
        description="Consumer Nostr x-only pubkey (64 hex) to enable a federated rating",
    )

    @field_validator("payer_pubkey")
    @classmethod
    def _validate_payer_pubkey(cls, v: str | None) -> str | None:
        if v is not None and not is_pubkey_hex(v):
            raise ValueError("payer_pubkey must be a 32-byte x-only pubkey (64 hex chars)")
        return v


class ConfirmExecutionRequest(BaseModel):
    payment_hash: str = Field(..., description="Payment hash proving payment")
    payment_preimage: str = Field(
        ..., description="Payment preimage (hex) - SHA256(preimage) must equal payment_hash"
    )


class SubmitRatingRequest(BaseModel):
    score: int = Field(..., ge=1, le=5, description="Rating 1-5")
    review: str = Field(default="", description="Optional review text")
    payment_preimage: str = Field(..., description="Preimage proving payment (hex)")
    signed_attestation: dict | None = Field(
        default=None,
        description="Optional consumer-signed kind-9070 rating event to federate",
    )


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("/skills")
async def discover_skills(
    keyword: str = Query(default="", max_length=100, description="Search keyword"),
    category: str = Query(default="", max_length=50, description="Filter by category"),
    max_price: int = Query(default=0, ge=0, description="Max price in sats (0=no limit)"),
    session: AsyncSession = Depends(get_session),
):
    """Discover skills by keyword, category, or price range."""
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
    skill = await _get_skill_or_404(session, skill_id)
    weighted_rating = await calculate_weighted_rating(session, skill.id)

    # Federation: cross-node reputation from cached attestations, preferred as the
    # primary score when it has independent ratings (local weighted_rating is the
    # fallback). use_web_of_trust=False keeps this a single indexed read.
    federated = None
    if settings.federation_enabled:
        try:
            agg = await get_cached_reputation(
                session,
                skill_id=str(skill.id),
                provider_pubkey=get_node_keypair().pubkey_hex,
                use_web_of_trust=False,
            )
            if agg.total_ratings > 0:
                federated = {
                    "score": agg.score,
                    "distinct_payers": agg.distinct_payers,
                    "total_ratings": agg.total_ratings,
                    "flags": agg.flags,
                }
        except Exception as e:
            # Degrade to the local score if the cache read fails (e.g. the
            # migration isn't applied yet) — a detail view must not 500 over this.
            print(f"[federation] cached reputation read failed: {e}", file=sys.stderr)

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
        "federated_reputation": federated,
        "primary_score": federated["score"] if federated else weighted_rating,
    }


@router.post("/skills", status_code=201)
async def register_skill(
    req: RegisterSkillRequest,
    session: AsyncSession = Depends(get_session),
):
    """Register a new skill on the marketplace."""
    # If a webhook is provided, validate it now. Conduit will POST the
    # payment preimage to this URL on every execution; we refuse to even
    # store a URL that points at internal services.
    if req.webhook_url:
        try:
            validate_outbound_url(req.webhook_url)
        except UnsafeURLError as e:
            print(f"[register_skill] rejected webhook_url: {e}", file=sys.stderr)
            raise HTTPException(
                status_code=400,
                detail="webhook_url rejected: must be a public HTTPS endpoint",
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


@router.delete("/skills/{skill_id}")
async def delete_skill(
    skill_id: str,
    provider_name: str = Query(..., description="Provider name (must match the skill's provider)"),
    session: AsyncSession = Depends(get_session),
):
    """
    Delete a skill and all its executions and ratings.

    Requires provider_name to match the skill owner (H2 ownership check).
    Deletes in order: ratings -> executions -> skill (no DB-level cascades).
    """
    skill = await _get_skill_or_404(session, skill_id)

    # H2: Ownership check — only the provider who registered the skill can delete it
    if skill.provider_name != provider_name:
        raise HTTPException(
            status_code=403,
            detail="Only the skill provider can delete this skill.",
        )

    # Delete ratings for all executions of this skill
    exec_result = await session.execute(
        select(SkillExecution.id).where(SkillExecution.skill_id == skill.id)
    )
    exec_ids = [row[0] for row in exec_result.all()]

    ratings_deleted = 0
    if exec_ids:
        from conduit.models.rating import Rating as RatingModel
        for eid in exec_ids:
            rating_result = await session.execute(
                select(RatingModel).where(RatingModel.execution_id == eid)
            )
            for rating in rating_result.scalars().all():
                await session.delete(rating)
                ratings_deleted += 1

    # Delete executions
    executions_deleted = 0
    for eid in exec_ids:
        exec_obj = await session.get(SkillExecution, eid)
        if exec_obj:
            await session.delete(exec_obj)
            executions_deleted += 1

    # Delete the skill
    await session.delete(skill)
    await session.commit()

    return {
        "deleted": True,
        "skill_id": skill_id,
        "skill_name": skill.name,
        "executions_deleted": executions_deleted,
        "ratings_deleted": ratings_deleted,
    }


@router.delete("/executions/{execution_id}")
async def delete_execution(
    execution_id: str,
    consumer_name: str = Query(
        ..., description="Consumer name (must match the execution's consumer)"
    ),
    session: AsyncSession = Depends(get_session),
):
    """Delete an execution and its ratings. Requires consumer_name ownership check (H2)."""
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

    # H2: Ownership check
    if execution.consumer_name != consumer_name:
        raise HTTPException(
            status_code=403,
            detail="Only the consumer who created this execution can delete it.",
        )

    # Delete ratings first
    ratings_deleted = 0
    from conduit.models.rating import Rating as RatingModel
    rating_result = await session.execute(
        select(RatingModel).where(RatingModel.execution_id == execution.id)
    )
    for rating in rating_result.scalars().all():
        await session.delete(rating)
        ratings_deleted += 1

    await session.delete(execution)
    await session.commit()

    return {
        "deleted": True,
        "execution_id": execution_id,
        "ratings_deleted": ratings_deleted,
    }


@router.post("/executions")
async def request_skill_execution(
    req: RequestExecutionRequest,
    session: AsyncSession = Depends(get_session),
):
    """Request execution of a skill — generates invoice(s) for payment."""
    skill = await _get_skill_or_404(session, req.skill_id)

    payment_request = None
    payment_hash = None
    fee_payment_request = None
    fee_payment_hash = None
    fee_breakdown = calculate_fee(skill.price_sats)

    if skill.price_sats > 0:
        lnd = get_lnd()

        # Invoice 1: skill price
        invoice = lnd.create_invoice(
            amount_msats=skill.price_sats * 1000,
            memo=f"Conduit skill: {skill.name}",
        )
        payment_request = invoice.payment_request
        payment_hash = invoice.payment_hash

        # Invoice 2: platform fee (if enabled and > 0)
        if fee_breakdown.fee_enabled:
            fee_invoice = lnd.create_invoice(
                amount_msats=fee_breakdown.platform_fee_sats * 1000,
                memo=f"Conduit platform fee: {skill.name}",
            )
            fee_payment_request = fee_invoice.payment_request
            fee_payment_hash = fee_invoice.payment_hash

    execution = SkillExecution(
        skill_id=skill.id,
        consumer_name=req.consumer_name,
        payer_pubkey=req.payer_pubkey,
        input_data=req.input_data,
        payment_hash=payment_hash,
        amount_sats=skill.price_sats,
        platform_fee_sats=fee_breakdown.platform_fee_sats,
        fee_payment_hash=fee_payment_hash,
        fee_payment_request=fee_payment_request,
        fee_settled=False,
        status=(
            ExecutionStatus.PENDING_PAYMENT
            if skill.price_sats > 0
            else ExecutionStatus.COMPLETED
        ),
    )
    session.add(execution)
    await session.commit()

    response = {
        "execution_id": str(execution.id),
        "skill_name": skill.name,
        "price_sats": skill.price_sats,
        "platform_fee_sats": fee_breakdown.platform_fee_sats,
        "total_cost_sats": fee_breakdown.total_consumer_cost_sats,
        "payment_request": payment_request,
        "payment_hash": payment_hash,
        "status": execution.status.value,
    }
    if fee_payment_hash:
        response["fee_payment_request"] = fee_payment_request
        response["fee_payment_hash"] = fee_payment_hash

    return response


@router.post("/executions/{execution_id}/confirm")
async def confirm_skill_execution(
    execution_id: str,
    req: ConfirmExecutionRequest,
    session: AsyncSession = Depends(get_session),
):
    """
    Confirm payment for an execution and trigger the webhook.

    Requires three proofs:
    1. payment_hash matches the execution record
    2. SHA256(preimage) == payment_hash (proves caller actually paid)
    3. LND confirms the invoice is settled

    Uses SELECT FOR UPDATE to prevent double-confirm races (H6).
    """
    try:
        exec_uuid = uuid.UUID(execution_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid execution ID")

    # Lock the row to prevent concurrent confirm calls (H6)
    result = await session.execute(
        select(SkillExecution)
        .where(SkillExecution.id == exec_uuid)
        .with_for_update()
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

    # C1: Verify preimage proves payment (SHA256(preimage) must equal payment_hash)
    try:
        preimage_bytes = bytes.fromhex(req.payment_preimage)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid preimage format (must be hex)")

    computed_hash = hashlib.sha256(preimage_bytes).hexdigest()
    if computed_hash != execution.payment_hash:
        raise HTTPException(
            status_code=400,
            detail="Payment preimage does not match payment hash. "
                   "SHA256(preimage) must equal the execution's payment_hash.",
        )

    # Verify skill invoice settled on LND
    lnd = get_lnd()
    try:
        invoice_status = lnd.lookup_invoice(execution.payment_hash)
    except Exception as e:
        print(f"[confirm] invoice lookup failed: {e}", file=sys.stderr)
        raise HTTPException(
            status_code=502, detail="Could not verify the invoice with the Lightning node"
        )

    if not invoice_status.get("settled"):
        raise HTTPException(
            status_code=402,
            detail={
                "error": "payment_not_settled",
                "payment_hash": execution.payment_hash,
                "message": "Pay the skill invoice on Lightning first, then retry confirm.",
            },
        )

    # Verify platform fee invoice settled (if applicable)
    if execution.fee_payment_hash and execution.platform_fee_sats > 0:
        try:
            fee_status = lnd.lookup_invoice(execution.fee_payment_hash)
        except Exception as e:
            print(f"[confirm] fee invoice lookup failed: {e}", file=sys.stderr)
            raise HTTPException(
                status_code=502, detail="Could not verify the fee invoice with the Lightning node"
            )

        if not fee_status.get("settled"):
            raise HTTPException(
                status_code=402,
                detail={
                    "error": "fee_not_settled",
                    "fee_payment_hash": execution.fee_payment_hash,
                    "fee_amount_sats": execution.platform_fee_sats,
                    "message": "Pay both the skill invoice and the fee invoice, then retry.",
                },
            )
        execution.fee_settled = True

    # Store the verified preimage and update status
    execution.payment_preimage = req.payment_preimage
    execution.status = ExecutionStatus.PAYMENT_RECEIVED
    execution.updated_at = datetime.now(UTC)

    # Look up the skill to get endpoint_url for webhook (C2)
    skill_result = await session.execute(
        select(Skill).where(Skill.id == execution.skill_id)
    )
    skill = skill_result.scalar_one_or_none()
    if not skill:
        execution.status = ExecutionStatus.FAILED
        execution.error_message = "Skill not found in registry"
        await session.commit()
        raise HTTPException(status_code=404, detail="Skill no longer exists in registry")

    # Federation: now that payment is settled, mint the provider's payer-binding
    # so the consumer can publish a verifiable rating (gated by FEDERATION_ENABLED
    # and the presence of a captured payer_pubkey).
    binding_sig = mint_execution_binding(
        skill_id=str(skill.id),
        payment_hash=execution.payment_hash,
        payer_pubkey=execution.payer_pubkey,
        provider_keypair=get_node_keypair(),
        enabled=settings.federation_enabled,
    )
    federation_info = None
    if binding_sig:
        execution.provider_binding_sig = binding_sig
        federation_info = {
            "provider_binding_sig": binding_sig,
            "provider_pubkey": get_node_keypair().pubkey_hex,
            "skill_id": str(skill.id),
        }

    # Run anomaly detection
    anomaly_flags = []
    try:
        anomaly_flags = await check_for_anomalies(
            payment_hash=execution.payment_hash,
            execution_id=str(execution.id),
            consumer_name=execution.consumer_name,
            provider_name=skill.provider_name,
            skill_id=str(skill.id),
            amount_sats=execution.amount_sats,
        )
    except Exception:
        pass  # Don't fail confirm over anomaly detection

    # C2: Execute via webhook if the skill has an endpoint
    if not skill.endpoint_url:
        execution.status = ExecutionStatus.COMPLETED
        execution.output_data = {
            "message": f"Payment of {execution.amount_sats} sats confirmed for '{skill.name}'.",
            "note": "No execution endpoint configured. Provider needs to register an endpoint_url.",
            "payment_proof": {
                "payment_hash": execution.payment_hash,
                "payment_preimage": req.payment_preimage,
            },
        }
        await session.commit()
        return {
            "execution_id": str(execution.id),
            "status": execution.status.value,
            "fee_settled": execution.fee_settled,
            "output": execution.output_data,
            "anomaly_flags": len(anomaly_flags),
            "federation": federation_info,
        }

    # Has a webhook — fire it
    execution.status = ExecutionStatus.EXECUTING
    await session.commit()

    try:
        webhook_result = await execute_skill_webhook(
            endpoint_url=skill.endpoint_url,
            input_data=execution.input_data or {},
            payment_hash=execution.payment_hash,
            payment_preimage=req.payment_preimage,
            skill_name=skill.name,
            execution_id=str(execution.id),
        )
        execution.status = ExecutionStatus.COMPLETED
        execution.output_data = webhook_result.get("output", webhook_result)
        execution.execution_time_ms = webhook_result.get("execution_time_ms")
        skill.total_executions = (skill.total_executions or 0) + 1
        await session.commit()

        return {
            "execution_id": str(execution.id),
            "status": execution.status.value,
            "fee_settled": execution.fee_settled,
            "output": execution.output_data,
            "execution_time_ms": execution.execution_time_ms,
            "anomaly_flags": len(anomaly_flags),
            "federation": federation_info,
        }

    except SkillExecutionError as e:
        execution.status = ExecutionStatus.FAILED
        execution.error_message = e.reason
        await session.commit()
        raise HTTPException(
            status_code=502,
            detail={
                "error": "skill_execution_failed",
                "execution_id": str(execution.id),
                "reason": e.reason,
                "message": (
                    "Payment received but skill execution failed. "
                    "Contact provider for refund."
                ),
            },
        )


@router.post("/executions/{execution_id}/rate")
async def submit_rating(
    execution_id: str,
    req: SubmitRatingRequest,
    background_tasks: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    """Rate a completed skill execution (requires payment preimage proof)."""
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
        comment=req.review,
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

    # Federation: publish the consumer-signed attestation (pre-signed mode) or
    # build it if this node is the payer. Best-effort — never fail the local rating.
    federation_result = None
    if settings.federation_enabled and execution.provider_binding_sig and execution.payer_pubkey:
        try:
            node = get_node_keypair()
            provider_pubkey = node.pubkey_hex
            skill_id_str = str(skill.id)
            event = None
            if req.signed_attestation:
                event = NostrEvent.from_dict(req.signed_attestation)
            elif execution.payer_pubkey == provider_pubkey:
                event = build_rating_attestation(
                    skill_id=skill_id_str,
                    provider_pubkey=provider_pubkey,
                    payment_hash=execution.payment_hash,
                    score=req.score,
                    payer_keypair=node,
                    provider_binding_sig=execution.provider_binding_sig,
                )
            if event is not None:
                cached = await submit_attestation(
                    session,
                    event,
                    skill_id=skill_id_str,
                    provider_pubkey=provider_pubkey,
                    payment_hash=execution.payment_hash,
                    payer_pubkey=execution.payer_pubkey,
                    expected_score=req.score,
                )
                if cached is not None:
                    await session.commit()
                    # Broadcast to relays OFF the request's hot path.
                    background_tasks.add_task(publish_rating, cached)
                    federation_result = {"event_id": cached.id, "published": "scheduled"}
        except Exception as e:
            # Roll back the poisoned transaction so the already-committed local
            # rating and the weighted query below aren't affected.
            await session.rollback()
            print(f"[federation] rating publish failed: {e}", file=sys.stderr)

    weighted = await calculate_weighted_rating(session, skill.id)

    return {
        "rating_id": str(rating.id),
        "score": req.score,
        "weighted_average": weighted,
        "execution_id": execution_id,
        "federation": federation_result,
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
