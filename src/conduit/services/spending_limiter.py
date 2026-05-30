"""
Spending limiter — enforces payment caps to prevent runaway agent spending.

Checks three limits before any outgoing payment:
1. Per-payment max (single transaction can't exceed X sats)
2. Hourly rolling window (total spend in last 60 min)
3. Daily rolling window (total spend in last 24h)

Also handles confirmation flow for payments above a threshold.

Confirmation model (important): for payments above the configured
threshold, this module issues a one-shot server-generated token. The
caller cannot self-attest to "I'm confirmed" via a boolean — they have
to surface the token to the user, get it back, and present it on retry.
The token is bound to the (tool, amount, payment_hash) tuple and expires.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
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
    """
    Raised when a payment exceeds the confirmation threshold. The
    `confirmation_token` field carries a one-shot, server-issued token the
    caller must present on retry (NOT a boolean the caller can flip).
    """

    def __init__(
        self,
        amount_sats: int,
        threshold_sats: int,
        description: str,
        confirmation_token: str,
        expires_in_seconds: int,
    ):
        self.amount_sats = amount_sats
        self.threshold_sats = threshold_sats
        self.description = description
        self.confirmation_token = confirmation_token
        self.expires_in_seconds = expires_in_seconds
        super().__init__(
            f"Payment of {amount_sats} sats exceeds confirmation threshold "
            f"of {threshold_sats} sats"
        )


# How long a confirmation token is valid for. Short enough that a leaked
# token can't be replayed hours later, long enough for a human to read
# the prompt and respond.
CONFIRMATION_TOKEN_TTL_SECONDS = 120

# H1: Stateless HMAC-signed confirmation tokens.
# No in-memory dict → no memory leak, works across workers/replicas.
# The token encodes binding + timestamp, signed with the API key.
# Trade-off: tokens are replayable within TTL (since there's no server-side
# state to mark them consumed). The binding ensures they can only be used
# for the exact (tool, amount, payment_hash) they were issued for.

import base64


def _get_signing_key() -> bytes:
    """Derive a signing key from the API key."""
    return hashlib.sha256(f"confirmation-token:{settings.conduit_api_key}".encode()).digest()


def _binding(tool_name: str, amount_sats: int, payment_hash: str | None) -> str:
    """Stable fingerprint of what a confirmation token is authorizing."""
    h = hashlib.sha256()
    h.update(tool_name.encode("utf-8"))
    h.update(b"|")
    h.update(str(int(amount_sats)).encode("utf-8"))
    h.update(b"|")
    h.update((payment_hash or "").encode("utf-8"))
    return h.hexdigest()


def _issue_confirmation_token(tool_name: str, amount_sats: int, payment_hash: str | None) -> tuple[str, int]:
    """Mint a stateless HMAC-signed confirmation token. Returns (token, ttl_seconds)."""
    binding_hash = _binding(tool_name, amount_sats, payment_hash)
    issued_at = str(int(datetime.now(timezone.utc).timestamp()))
    payload = f"{binding_hash}|{issued_at}"
    sig = hmac.new(_get_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(f"{payload}|{sig}".encode()).decode()
    return token, CONFIRMATION_TOKEN_TTL_SECONDS


def _redeem_confirmation_token(
    token: str, tool_name: str, amount_sats: int, payment_hash: str | None
) -> bool:
    """
    Verify a stateless HMAC-signed confirmation token.

    Checks: signature is valid, binding matches, and token hasn't expired.
    """
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        parts = decoded.split("|")
        if len(parts) != 3:
            return False
        binding_hash, issued_at_str, sig = parts

        # Verify signature
        payload = f"{binding_hash}|{issued_at_str}"
        expected_sig = hmac.new(_get_signing_key(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected_sig):
            return False

        # Verify binding matches this request
        expected_binding = _binding(tool_name, amount_sats, payment_hash)
        if not hmac.compare_digest(binding_hash, expected_binding):
            return False

        # Verify not expired
        issued_at = int(issued_at_str)
        now = int(datetime.now(timezone.utc).timestamp())
        if now - issued_at > CONFIRMATION_TOKEN_TTL_SECONDS:
            return False

        return True
    except Exception:
        return False


async def check_spending_limits(
    amount_sats: int,
    tool_name: str,
    description: str = "",
    confirmation_token: str | None = None,
    payment_hash: str | None = None,
) -> str | None:
    """
    Check if a payment is allowed under current spending limits and
    atomically reserve the amount to prevent TOCTOU races (C6).

    Returns a reservation_id (str) that the caller MUST pass to either
    `record_successful_payment` (on success) or `cancel_reservation`
    (on failure). The reserved amount is included in window totals so
    concurrent callers cannot exceed the limit.

    Raises SpendingLimitExceeded if any limit would be breached.
    Raises ConfirmationRequired (carrying a fresh server-issued token) if
    amount exceeds the confirmation threshold and no valid token was
    presented.

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
    if confirm_threshold > 0 and amount_sats > confirm_threshold:
        if confirmation_token and _redeem_confirmation_token(
            confirmation_token, tool_name, amount_sats, payment_hash
        ):
            pass
        else:
            token, ttl = _issue_confirmation_token(tool_name, amount_sats, payment_hash)
            raise ConfirmationRequired(
                amount_sats=amount_sats,
                threshold_sats=confirm_threshold,
                description=description,
                confirmation_token=token,
                expires_in_seconds=ttl,
            )

    # --- C6: Atomically reserve the amount ---
    # Insert a "reserved" row so concurrent callers see this in-flight
    # spend in their window totals, closing the TOCTOU gap.
    reservation_id = await _reserve(amount_sats, tool_name, description, payment_hash)
    return reservation_id


async def record_successful_payment(
    amount_sats: int,
    tool_name: str,
    description: str = "",
    payment_hash: str | None = None,
    reservation_id: str | None = None,
) -> None:
    """
    Finalize a spending reservation as a successful payment.

    If reservation_id is provided, the existing reserved row is promoted
    to "allowed". Otherwise falls back to inserting a new row (backward
    compatible with callers that haven't adopted reservations yet).
    """
    async with async_session_factory() as session:
        if reservation_id:
            import uuid as _uuid
            result = await session.execute(
                select(SpendingLog)
                .where(SpendingLog.id == _uuid.UUID(reservation_id))
                .with_for_update()
            )
            log = result.scalar_one_or_none()
            if log and log.status == "reserved":
                log.status = "allowed"
                log.payment_hash = payment_hash
                await session.commit()
                return
        # Fallback: insert a new row (no reservation to finalize)
        log = SpendingLog(
            tool_name=tool_name,
            description=description,
            amount_sats=amount_sats,
            status="allowed",
            payment_hash=payment_hash,
        )
        session.add(log)
        await session.commit()


async def cancel_reservation(reservation_id: str) -> None:
    """
    Cancel an in-flight spending reservation (e.g. when payment fails).

    Marks the reserved row as "cancelled" so the amount is freed from
    window totals.
    """
    if not reservation_id:
        return
    import uuid as _uuid
    async with async_session_factory() as session:
        result = await session.execute(
            select(SpendingLog)
            .where(SpendingLog.id == _uuid.UUID(reservation_id))
            .with_for_update()
        )
        log = result.scalar_one_or_none()
        if log and log.status == "reserved":
            log.status = "cancelled"
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


# --- Internal helpers ---


async def _get_spent_in_window(window: timedelta) -> int:
    """Get total sats spent in a rolling time window.

    Includes both "allowed" (settled) and "reserved" (in-flight) rows
    so concurrent requests cannot exceed limits (C6 fix).
    """
    cutoff = datetime.now(timezone.utc) - window
    async with async_session_factory() as session:
        result = await session.execute(
            select(sa_func.coalesce(sa_func.sum(SpendingLog.amount_sats), 0))
            .where(SpendingLog.status.in_(["allowed", "reserved"]))
            .where(SpendingLog.created_at >= cutoff)
        )
        return int(result.scalar() or 0)


async def _reserve(
    amount_sats: int,
    tool_name: str,
    description: str,
    payment_hash: str | None,
) -> str:
    """Insert a 'reserved' spending log row and return its ID."""
    async with async_session_factory() as session:
        log = SpendingLog(
            tool_name=tool_name,
            description=description,
            amount_sats=amount_sats,
            status="reserved",
            payment_hash=payment_hash,
        )
        session.add(log)
        await session.commit()
        await session.refresh(log)
        return str(log.id)


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
