"""Invoice endpoints — create and track Lightning invoices."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.database import get_db
from conduit.models.invoice import Invoice, InvoiceStatus
from conduit.models.wallet import Wallet
from conduit.schemas.invoice import InvoiceCreate, InvoiceResponse
from conduit.services.lnd import lnd_client

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post("/", response_model=InvoiceResponse, status_code=201)
async def create_invoice(
    data: InvoiceCreate,
    db: AsyncSession = Depends(get_db),
) -> Invoice:
    """
    Create a new Lightning invoice for receiving payment.

    This generates a BOLT-11 invoice via LND and stores it in the database.
    """
    # Verify wallet exists
    result = await db.execute(select(Wallet).where(Wallet.id == data.wallet_id))
    wallet = result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    # Create invoice via LND
    lnd_invoice = await lnd_client.create_invoice(
        amount_msats=data.amount_msats,
        memo=data.memo or "",
        expiry=data.expiry_seconds,
    )

    # Persist to database
    invoice = Invoice(
        wallet_id=wallet.id,
        payment_request=lnd_invoice.payment_request,
        payment_hash=lnd_invoice.payment_hash,
        amount_msats=data.amount_msats,
        memo=data.memo,
        expiry_seconds=data.expiry_seconds,
        status=InvoiceStatus.PENDING,
    )
    db.add(invoice)
    await db.flush()
    await db.refresh(invoice)
    return invoice


@router.get("/{invoice_id}", response_model=InvoiceResponse)
async def get_invoice(
    invoice_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Invoice:
    """Get invoice details by ID."""
    result = await db.execute(select(Invoice).where(Invoice.id == invoice_id))
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice


@router.get("/hash/{payment_hash}", response_model=InvoiceResponse)
async def get_invoice_by_hash(
    payment_hash: str,
    db: AsyncSession = Depends(get_db),
) -> Invoice:
    """Look up an invoice by its payment hash."""
    result = await db.execute(
        select(Invoice).where(Invoice.payment_hash == payment_hash)
    )
    invoice = result.scalar_one_or_none()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice
