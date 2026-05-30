"""Lightning Network endpoints — mirrors MCP Lightning tools over HTTP.

6 endpoints:
  GET  /api/v1/lightning/node-info
  GET  /api/v1/lightning/balance
  POST /api/v1/lightning/invoices
  POST /api/v1/lightning/invoices/decode
  POST /api/v1/lightning/payments
  GET  /api/v1/lightning/payments/{payment_hash}
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from conduit.api.deps import verify_api_key, get_lnd
from conduit.services.spending_limiter import (
    cancel_reservation,
    check_spending_limits,
    record_successful_payment,
    SpendingLimitExceeded,
    ConfirmationRequired,
    get_spending_summary,
)
from conduit.services.anomaly_detector import check_for_anomalies

router = APIRouter(
    prefix="/lightning",
    tags=["lightning"],
    dependencies=[Depends(verify_api_key)],
)


# ── Request / Response models ─────────────────────────────────────────


class CreateInvoiceRequest(BaseModel):
    amount_sats: int = Field(..., gt=0, description="Invoice amount in satoshis")
    memo: str = Field(default="", description="Human-readable description")
    expiry_seconds: int = Field(default=3600, description="Seconds until invoice expires")


class PayInvoiceRequest(BaseModel):
    payment_request: str = Field(..., description="BOLT-11 encoded invoice")
    max_fee_sats: int = Field(default=10, description="Maximum routing fee in sats")
    confirmation_token: str | None = Field(
        default=None,
        description=(
            "Server-issued one-shot token from a prior call that returned 402. "
            "Bound to (tool, amount, payment_hash). Required for any amount "
            "above SPENDING_CONFIRM_ABOVE_SATS."
        ),
    )


class DecodeInvoiceRequest(BaseModel):
    payment_request: str = Field(..., description="BOLT-11 encoded invoice to decode")


# ── Endpoints ─────────────────────────────────────────────────────────


@router.get("/node-info")
async def get_node_info():
    """Get Lightning node info — alias, pubkey, channels, sync status."""
    lnd = get_lnd()
    info = lnd.get_info()
    return {
        "alias": info.alias,
        "pubkey": info.pubkey,
        "num_active_channels": info.num_active_channels,
        "num_peers": info.num_peers,
        "block_height": info.block_height,
        "synced_to_chain": info.synced_to_chain,
        "version": info.version,
    }


@router.get("/balance")
async def get_balance():
    """Get channel and on-chain balances in satoshis."""
    lnd = get_lnd()
    bal = lnd.get_balance()
    return bal


@router.post("/invoices")
async def create_invoice(req: CreateInvoiceRequest):
    """Create a Lightning invoice for receiving payment."""
    lnd = get_lnd()
    invoice = lnd.create_invoice(
        amount_msats=req.amount_sats * 1000,
        memo=req.memo,
        expiry=req.expiry_seconds,
    )
    return {
        "payment_request": invoice.payment_request,
        "payment_hash": invoice.payment_hash,
        "amount_sats": req.amount_sats,
    }


@router.post("/invoices/decode")
async def decode_invoice(req: DecodeInvoiceRequest):
    """Decode a BOLT-11 invoice without paying it."""
    lnd = get_lnd()
    decoded = lnd.decode_invoice(req.payment_request)
    return decoded


@router.post("/payments")
async def pay_invoice(req: PayInvoiceRequest):
    """Pay a Lightning invoice (with spending limit enforcement)."""
    lnd = get_lnd()

    # Decode to get amount
    decoded = lnd.decode_invoice(req.payment_request)
    amount_sats = decoded["amount_sats"]
    description = decoded.get("description", "") or "Lightning payment"
    invoice_payment_hash = decoded.get("payment_hash") or ""

    # M2: Reject zero-amount (any-amount) invoices — spending check would
    # pass vacuously and LND would reject anyway without an amt field.
    if amount_sats <= 0:
        raise HTTPException(
            status_code=400,
            detail="Zero-amount invoices are not supported. The invoice must specify an amount.",
        )

    # Check spending limits and reserve amount atomically (C6)
    reservation_id = None
    try:
        reservation_id = await check_spending_limits(
            amount_sats=amount_sats,
            tool_name="pay_invoice",
            description=description,
            confirmation_token=req.confirmation_token,
            payment_hash=invoice_payment_hash,
        )
    except SpendingLimitExceeded as e:
        raise HTTPException(status_code=403, detail={
            "error": "spending_limit_exceeded",
            "reason": e.reason,
            "limit_sats": e.limit_sats,
            "current_sats": e.current_sats,
            "requested_sats": e.requested_sats,
        })
    except ConfirmationRequired as e:
        raise HTTPException(status_code=402, detail={
            "error": "confirmation_required",
            "amount_sats": e.amount_sats,
            "threshold_sats": e.threshold_sats,
            "description": e.description,
            "confirmation_token": e.confirmation_token,
            "expires_in_seconds": e.expires_in_seconds,
            "message": (
                "Surface this token to the user, get approval, then resend "
                "with confirmation_token set. The token is bound to this "
                "(amount, payment_hash) and is single-use."
            ),
        })

    # Execute payment
    result = lnd.pay_invoice(
        payment_request=req.payment_request,
        max_fee_msats=req.max_fee_sats * 1000,
    )

    if result.status == "SUCCEEDED":
        # Bookkeeping (non-critical)
        try:
            await record_successful_payment(
                amount_sats=amount_sats,
                tool_name="pay_invoice",
                description=description,
                payment_hash=result.payment_hash,
                reservation_id=reservation_id,
            )
            await check_for_anomalies(
                payment_hash=result.payment_hash,
                amount_sats=amount_sats,
            )
        except Exception as e:
            # H5: Log the failure instead of silently swallowing. The reservation
            # stays as "reserved" so spending limits remain conservative.
            import sys
            print(f"[pay_invoice] Bookkeeping error (payment DID succeed): {e}", file=sys.stderr)

        return {
            "status": "SUCCEEDED",
            "payment_hash": result.payment_hash,
            "preimage": result.preimage,
            "amount_sats": amount_sats,
            "fee_msats": result.fee_msats,
        }
    else:
        # Payment failed — release the reservation
        if reservation_id:
            try:
                await cancel_reservation(reservation_id)
            except Exception:
                pass
        raise HTTPException(status_code=502, detail={
            "status": "FAILED",
            "payment_hash": result.payment_hash,
            "reason": result.failure_reason,
        })


@router.get("/payments/{payment_hash}")
async def check_payment(payment_hash: str):
    """Check if a payment has settled."""
    lnd = get_lnd()
    try:
        result = lnd.lookup_invoice(payment_hash)
        return result
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Payment not found: {e}")
