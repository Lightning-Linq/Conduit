"""Wallet endpoints — create and manage agent wallets."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.database import get_db
from conduit.models.wallet import Wallet
from conduit.schemas.wallet import WalletCreate, WalletBalance, WalletResponse

router = APIRouter(prefix="/wallets", tags=["wallets"])


@router.post("/", response_model=WalletResponse, status_code=201)
async def create_wallet(
    data: WalletCreate,
    db: AsyncSession = Depends(get_db),
) -> Wallet:
    """Create a new wallet for an AI agent."""
    # Check for duplicate owner
    existing = await db.execute(
        select(Wallet).where(Wallet.owner_id == data.owner_id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Wallet already exists for this owner")

    wallet = Wallet(owner_id=data.owner_id, label=data.label)
    db.add(wallet)
    await db.flush()
    await db.refresh(wallet)
    return wallet


@router.get("/{wallet_id}", response_model=WalletResponse)
async def get_wallet(
    wallet_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> Wallet:
    """Get wallet details by ID."""
    result = await db.execute(select(Wallet).where(Wallet.id == wallet_id))
    wallet = result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")
    return wallet


@router.get("/{wallet_id}/balance", response_model=WalletBalance)
async def get_wallet_balance(
    wallet_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get the current balance of a wallet."""
    result = await db.execute(select(Wallet).where(Wallet.id == wallet_id))
    wallet = result.scalar_one_or_none()
    if not wallet:
        raise HTTPException(status_code=404, detail="Wallet not found")

    return {
        "wallet_id": wallet.id,
        "balance_msats": wallet.balance_msats,
        "balance_sats": wallet.balance_msats // 1000,
        "balance_btc": wallet.balance_msats / 100_000_000_000,
    }
