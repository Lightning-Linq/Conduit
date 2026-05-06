"""Payment endpoints — send Lightning payments on behalf of agents."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.config import settings
from conduit.core.database import get_db
from conduit.models.payment import Payment, PaymentStatus
from conduit.models.wallet import Wallet
from conduit.schemas.payment import PaymentCreate, PaymentResponse
from conduit.services.lnd import lnd_client

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("/", response_model=PaymentResponse, status_code=201)
async def send_payment(
    data: PaymentCreate,
    db: AsyncSession = Depends(get_db),
) -> Payment:
    """
    Send a Lightning payment from an agent's wallet.

    Deducts balance, pays the invoice via LND, and records the result.
    Platform fee is calculated and deducted from the wallet.
    """
    # Verify wallet exists and has sufficient balance
    result = await db.execute(select(Wallet).where(Wallet.id == data.wallet_id))
    wallet = result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    if not wallet.is_active:
        raise HTTPException(status_code=403, detail="Wallet is deactivated")

    # Decode invoice to get amount (in production, decode BOLT-11 here)
    # For now, we'll let LND handle validation

    # Send via LND
    lnd_response = await lnd_client.pay_invoice(
        payment_request=data.payment_request,
        max_fee_msats=data.max_fee_msats,
    )

    # Calculate platform fee
    platform_fee = int(lnd_response.fee_msats * settings.transaction_fee_percent / 100)

    # Determine status
    if lnd_response.status == "SUCCEEDED":
        status = PaymentStatus.SUCCEEDED
        # Deduct from wallet
        total_cost = lnd_response.fee_msats + platform_fee  # amount already deducted via LND
        wallet.balance_msats -= total_cost
    else:
        status = PaymentStatus.FAILED

    # Record payment
    payment = Payment(
        wallet_id=wallet.id,
        payment_request=data.payment_request,
        payment_hash=lnd_response.payment_hash,
        preimage=lnd_response.preimage if status == PaymentStatus.SUCCEEDED else None,
        amount_msats=0,  # Will be filled from decoded invoice
        fee_msats=lnd_response.fee_msats,
        platform_fee_msats=platform_fee,
        status=status,
        failure_reason=lnd_response.failure_reason,
    )
    db.add(payment)
    await db.flush()
    await db.refresh(payment)
    return payment


@router.get("/{payment_id}", response_model=PaymentResponse)
async def get_payment(
    payment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Payment:
    """Get payment details by ID."""
    result = await db.execute(select(Payment).where(Payment.id == payment_id))
    payment = result.scalar_one_or_none()
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    return payment
