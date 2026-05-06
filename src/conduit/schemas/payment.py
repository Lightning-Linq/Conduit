"""Pydantic schemas for payment API requests/responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class PaymentCreate(BaseModel):
    """Request to send a Lightning payment."""

    wallet_id: uuid.UUID
    payment_request: str = Field(..., description="BOLT-11 invoice to pay")
    max_fee_msats: int = Field(default=10000, ge=0, description="Max routing fee in msats")


class PaymentResponse(BaseModel):
    """Payment data returned by the API."""

    id: uuid.UUID
    wallet_id: uuid.UUID
    payment_hash: str
    amount_msats: int
    fee_msats: int
    platform_fee_msats: int
    status: str
    failure_reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
