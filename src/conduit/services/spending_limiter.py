"""
Spending limiter — enforces payment caps to prevent runaway agent spending.

Checks three limits before any outgoing payment:
1. Per-payment max (single transaction can't exceed X sats)
2. Hourly rolling window (total spend in last 60 min)
3. Daily rolling window (total spend in last 24h)

Also handles confirmation flow for payments above a threshold.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func as sa_func
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.config import settings
from conduit.core.database import async_session_factory
from conduit.models.spending_log import SpendingLog


class SpendingLimitExceeded(Exception):
    """Raised when a payment would exceed configured spending limits."""

    def __init__(self, reason: str, limit_sats: int, current_sats: int, requested_sats: int):
        self.reason = reason
        self.limit_sats = limit_sats
        self.current_sats = current_sats
        self.requested_sats = requested_sats
        super().__init__(reason)


class ConfirmationRequired(Exception):
    """Raised when a payment exceeds the confirmation threshold."""

    def __init__(self, amount_sats: int, threshold_sats: int, description: str):
        self.amount_sats = amount_sats
        self.threshold_sats = threshold_sats
        self.description = description
        super().__init__(f"Payment of {amount_sats} sats exceeds confirmation threshold of {threshold_sats} sats")


# In-memory store for pending confirmations (keyed by payment description hash)
_pending_confirmations: dict[str, dict] = {}


async def check_spending_limits(
    amount_sats: int,
    tool_name: str,
    description: str = "",
    confirmed: bool = False,
) -> None:
    """
    Check if a payment is allowed under current spending limits.

    Raises SpendingLimitExceeded if any limit would be breached.
    Raises ConfirmationRequired if amount exceeds confirmation threshold
    and confirmed=False.

    Call this BEFORE executing any outgoing payment.
    """
    # --- Check 1: Per-payment limit ---
    per_payment_limit = settings.spending_limit_per_payment_sats
    if per_payment_limit > 0 and amount_sats > per_payment_limit:
        await _log_blocked(amount_sats, tool_name, description,
                           f"Exceeds per-payment limit of {per_payment_limit} sats")
        raise SpendingLimitExceeded(
            reason=f"Payment of {amount_sats:,} sats exceeds per-payment limit of {per_payment_limit:,} sats",
            limit_sats=per_payment_limit,
            current_sats=0,
            requested_sats=amount_sats,
        )

    # --- Check 2: Hourly rolling window ---
    hourly_limit = settings.spending_limit_hourly_sats
    if hourly_limit > 0:
        spent_last_hour = await _get_spent_in_window(timedelta(hours=1))
        if spent_last_hour + amount_sats > hourly_limit:
            await _log_blocked(amount_sats, tool_name, description,
                               f"Would exceed hourly limit ({spent_last_hour} + {amount_sats} > {hourly_limit})")
            raise SpendingLimitExceeded(
                reason=(
                    f"Payment of {amount_sats:,} sats would exceed hourly limit of {hourly_limit:,} sats. "
                    f"Already spent {spent_last_hour:,} sats in the last hour."
                ),
                limit_sats=hourly_limit,
                current_sats=spent_last_hour,
                requested_sats=amount_sats,
            )

    # --- Check 3: Daily rolling window ---
    daily_limit = settings.spending_limit_daily_sats
    if daily_limit > 0:
        spent_last_day = await _get_spent_in_window(timedelta(hours=24))
        if spent_last_day + amount_sats > daily_limit:
            await _log_blocked(amount_sats, tool_name, description,
                               f"Would exceed daily limit ({spent_last_day} + {amount_sats} > {daily_limit})")
            raise SpendingLimitExceeded(
                reason=(
                    f"Payment of {amount_sats:,} sats would exceed daily limit of {daily_limit:,} sats. "
                    f"Already spent {spent_last_day:,} sats in the last 24 hours."
                ),
                limit_sats=daily_limit,
                current_sats=spent_last_day,
                requested_sats=amount_sats,
            )

    # --- Check 4: Confirmation threshold ---
    confirm_threshold = settings.spending_confirm_above_sats
    if confirm_threshold > 0 and amount_sats > confirm_threshold and not confirmed:
        # Store pending confirmation
        confirm_key = f"{tool_name}:{amount_sats}:{description}"
        _pending_confirmations[confirm_key] = {
            "amount_sats": amount_sats,
            "tool_name": tool_name,
            "description": description,
            "created_at": datetime.now(timezone.utc),
        }
        raise ConfirmationRequired(
            amount_sats=amount_sats,
            threshold_sats=confirm_threshold,
            description=description,
        )


async def record_successful_payment(
    amount_sats: int,
    tool_name: str,
    description: str = "",
    payment_hash: str | None = None,
) -> None:
    """Record a successful payment in the spending log."""
    async with async_session_factory() as session:
        log = SpendingLog(
            tool_name=tool_name,
            description=description,
            amount_sats=amount_sats,
            status="allowed",
            payment_hash=payment_hash,
        )
        session.add(log)
        await session.commit()


async def get_spending_summary() -> dict:
    """Get current spending status for display."""
    spent_hour = await _get_spent_in_window(timedelta(hours=1))
    spent_day = await _get_spent_in_window(timedelta(hours=24))

    return {
        "spent_last_hour_sats": spent_hour,
        "spent_last_24h_sats": spent_day,
        "hourly_limit_sats": settings.spending_limit_hourly_sats,
        "daily_limit_sats": settings.spending_limit_daily_sats,
        "per_payment_limit_sats": settings.spending_limit_per_payment_sats,
        "confirm_threshold_sats": settings.spending_confirm_above_sats,
        "hourly_remaining_sats": max(0, settings.spending_limit_hourly_sats - spent_hour),
        "daily_remaining_sats": max(0, settings.spending_limit_daily_sats - spent_day),
    }


def check_confirmation(tool_name: str, amount_sats: int, description: str) -> bool:
    """Check if a pending confirmation exists for this payment."""
    confirm_key = f"{tool_name}:{amount_sats}:{description}"
    if confirm_key in _pending_confirmations:
        del _pending_confirmations[confirm_key]
        return True
    return False


# --- Internal helpers ---


async def _get_spent_in_window(window: timedelta) -> int:
    """Get total sats spent in a rolling time window (only successful payments)."""
    cutoff = datetime.now(timezone.utc) - window
    async with async_session_factory() as session:
        result = await session.execute(
            select(sa_func.coalesce(sa_func.sum(SpendingLog.amount_sats), 0))
            .where(SpendingLog.status == "allowed")
            .where(SpendingLog.created_at >= cutoff)
        )
        return int(result.scalar() or 0)


async def _log_blocked(
    amount_sats: int,
    tool_name: str,
    description: str,
    reason: str,
) -> None:
    """Log a blocked payment attempt."""
    async with async_session_factory() as session:
        log = SpendingLog(
            tool_name=tool_name,
            description=description,
            amount_sats=amount_sats,
            status="blocked",
            block_reason=reason,
        )
        session.add(log)
        await session.commit()
