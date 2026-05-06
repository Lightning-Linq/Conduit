"""Pydantic schemas for wallet API requests/responses."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class WalletCreate(BaseModel):
    """Request to create a new wallet."""

    owner_id: str = Field(..., description="Unique identifier for the agent/owner")
    label: str | None = Field(None, description="Human-readable label for the wallet")


class WalletResponse(BaseModel):
    """Wallet data returned by the API."""

    id: uuid.UUID
    owner_id: str
    label: str | None
    balance_msats: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class WalletBalance(BaseModel):
    """Balance summary for a wallet."""

    wallet_id: uuid.UUID
    balance_msats: int
    balance_sats: int
    balance_btc: float
