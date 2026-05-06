"""Pydantic schemas for invoice API requests/responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class InvoiceCreate(BaseModel):
    """Request to create a new Lightning invoice."""

    wallet_id: uuid.UUID
    amount_msats: int = Field(..., gt=0, description="Amount in millisatoshis")
    memo: str | None = Field(None, max_length=512, description="Invoice description")
    expiry_seconds: int = Field(default=3600, ge=60, le=86400)


class InvoiceResponse(BaseModel):
    """Invoice data returned by the API."""

    id: uuid.UUID
    wallet_id: uuid.UUID
    payment_request: str
    payment_hash: str
    amount_msats: int
    amount_paid_msats: int
    memo: str | None
    status: str
    expiry_seconds: int
    created_at: datetime

    model_config = {"from_attributes": True}
