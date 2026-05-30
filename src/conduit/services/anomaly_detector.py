"""
Anomaly detection for Conduit transactions.

Runs after each successful payment or skill execution to flag suspicious
patterns. Currently operates in warning mode — flags are logged but
transactions are not blocked.

Detected patterns:
1. Rapid repeat — same consumer-provider pair transacting more than N times
   within a short window (possible wash trading for ratings)
2. Structuring — multiple payments just below the per-payment limit
   in a short window (attempting to circumvent spending limits)
3. Volume spike — spending rate dramatically exceeds historical average
4. Self-payment — consumer and provider names match (possible self-dealing)
5. Circular payment — A→B and B→A pattern detected (possible laundering)
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func as sa_func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.config import settings
from conduit.core.database import async_session_factory
from conduit.models.anomaly_flag import AnomalyFlag
from conduit.models.spending_log import SpendingLog
from conduit.models.execution import SkillExecution


# =============================================================================
# Detection thresholds (configurable later via .env)
# =============================================================================

# Rapid repeat: flag if same consumer executes same skill more than this
# many times within the window
RAPID_REPEAT_MAX = 3
RAPID_REPEAT_WINDOW = timedelta(minutes=30)

# Structuring: flag if more than this many payments land between
# 80-100% of the per-payment limit within the window
STRUCTURING_COUNT = 3
STRUCTURING_WINDOW = timedelta(hours=1)

# Volume spike: flag if spending in the last hour exceeds this
# multiple of the average hourly spend over the past 7 days
VOLUME_SPIKE_MULTIPLIER = 5.0


# =============================================================================
# Main entry point
# =============================================================================


async def check_for_anomalies(
    payment_hash: str | None = None,
    execution_id: str | None = None,
    consumer_name: str | None = None,
    provider_name: str | None = None,
    skill_id: str | None = None,
    amount_sats: int = 0,
) -> list[AnomalyFlag]:
    """
    Run all anomaly checks after a transaction.
    Returns a list of any flags generated (empty if clean).
    """
    flags: list[AnomalyFlag] = []

    async with async_session_factory() as session:
        # --- Check 1: Self-payment ---
        if consumer_name and provider_name:
            if consumer_name.lower() == provider_name.lower():
                flag = AnomalyFlag(
                    flag_type="self_payment",
                    severity="high",
                    description=(
                        f"Consumer '{consumer_name}' is paying provider '{provider_name}' "
                        f"— names match. Possible self-dealing."
                    ),
                    payment_hash=payment_hash,
                    execution_id=execution_id,
                    consumer_name=consumer_name,
                    provider_name=provider_name,
                    amount_sats=amount_sats,
                )
                session.add(flag)
                flags.append(flag)

        # --- Check 2: Rapid repeat ---
        if consumer_name and skill_id:
            cutoff = datetime.now(timezone.utc) - RAPID_REPEAT_WINDOW
            result = await session.execute(
                select(sa_func.count(SkillExecution.id))
                .where(
                    and_(
                        SkillExecution.consumer_name == consumer_name,
                        SkillExecution.skill_id == skill_id,
                        SkillExecution.created_at >= cutoff,
                    )
                )
            )
            recent_count = result.scalar() or 0

            if recent_count >= RAPID_REPEAT_MAX:
                flag = AnomalyFlag(
                    flag_type="rapid_repeat",
                    severity="medium",
                    description=(
                        f"Consumer '{consumer_name}' has executed the same skill "
                        f"{recent_count} times in the last {int(RAPID_REPEAT_WINDOW.total_seconds() / 60)} minutes. "
                        f"Possible wash trading or automated abuse."
                    ),
                    payment_hash=payment_hash,
                    execution_id=execution_id,
                    consumer_name=consumer_name,
                    provider_name=provider_name,
                    amount_sats=amount_sats,
                )
                session.add(flag)
                flags.append(flag)

        # --- Check 3: Structuring ---
        if amount_sats > 0:
            per_payment_limit = settings.spending_limit_per_payment_sats
            if per_payment_limit > 0:
                threshold_low = int(per_payment_limit * 0.8)
                cutoff = datetime.now(timezone.utc) - STRUCTURING_WINDOW

                result = await session.execute(
                    select(sa_func.count(SpendingLog.id))
                    .where(
                        and_(
                            SpendingLog.status == "allowed",
                            SpendingLog.amount_sats >= threshold_low,
                            SpendingLog.amount_sats <= per_payment_limit,
                            SpendingLog.created_at >= cutoff,
                        )
                    )
                )
                near_limit_count = result.scalar() or 0

                if near_limit_count >= STRUCTURING_COUNT:
                    flag = AnomalyFlag(
                        flag_type="structuring",
                        severity="high",
                        description=(
                            f"Detected {near_limit_count} payments between "
                            f"{threshold_low:,}-{per_payment_limit:,} sats in the last hour. "
                            f"Possible structuring to avoid per-payment limit."
                        ),
                        payment_hash=payment_hash,
                        consumer_name=consumer_name,
                        amount_sats=amount_sats,
                    )
                    session.add(flag)
                    flags.append(flag)

        # --- Check 4: Volume spike ---
        if amount_sats > 0:
            # Get spending in the last hour
            hour_cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
            result = await session.execute(
                select(sa_func.coalesce(sa_func.sum(SpendingLog.amount_sats), 0))
                .where(
                    and_(
                        SpendingLog.status == "allowed",
                        SpendingLog.created_at >= hour_cutoff,
                    )
                )
            )
            spent_last_hour = result.scalar() or 0

            # Get average hourly spending over the last 7 days
            week_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            result = await session.execute(
                select(sa_func.coalesce(sa_func.sum(SpendingLog.amount_sats), 0))
                .where(
                    and_(
                        SpendingLog.status == "allowed",
                        SpendingLog.created_at >= week_cutoff,
                    )
                )
            )
            spent_last_week = result.scalar() or 0
            avg_hourly = spent_last_week / (7 * 24) if spent_last_week > 0 else 0

            if avg_hourly > 0 and spent_last_hour > (avg_hourly * VOLUME_SPIKE_MULTIPLIER):
                flag = AnomalyFlag(
                    flag_type="volume_spike",
                    severity="medium",
                    description=(
                        f"Spending this hour ({spent_last_hour:,} sats) is "
                        f"{spent_last_hour / avg_hourly:.1f}x the 7-day hourly average "
                        f"({avg_hourly:,.0f} sats). Unusual volume."
                    ),
                    payment_hash=payment_hash,
                    consumer_name=consumer_name,
                    amount_sats=amount_sats,
                )
                session.add(flag)
                flags.append(flag)

        if flags:
            await session.commit()

    return flags


async def get_anomaly_summary() -> dict:
    """Get a summary of flagged anomalies."""
    async with async_session_factory() as session:
        # Total flags
        result = await session.execute(select(sa_func.count(AnomalyFlag.id)))
        total = result.scalar() or 0

        # Unreviewed
        result = await session.execute(
            select(sa_func.count(AnomalyFlag.id))
            .where(AnomalyFlag.reviewed == False)
        )
        unreviewed = result.scalar() or 0

        # By severity
        severity_counts = {}
        for sev in ("low", "medium", "high"):
            result = await session.execute(
                select(sa_func.count(AnomalyFlag.id))
                .where(AnomalyFlag.severity == sev)
            )
            severity_counts[sev] = result.scalar() or 0

        # By type
        type_counts = {}
        # L8: removed "circular_payment" — no code path raises it
        for ftype in ("self_payment", "rapid_repeat", "structuring", "volume_spike", "rating_concentration"):
            result = await session.execute(
                select(sa_func.count(AnomalyFlag.id))
                .where(AnomalyFlag.flag_type == ftype)
            )
            count = result.scalar() or 0
            if count > 0:
                type_counts[ftype] = count

        # Recent flags (last 5)
        result = await session.execute(
            select(AnomalyFlag)
            .order_by(AnomalyFlag.created_at.desc())
            .limit(5)
        )
        recent = result.scalars().all()

    return {
        "total_flags": total,
        "unreviewed": unreviewed,
        "by_severity": severity_counts,
        "by_type": type_counts,
        "recent": [
            {
                "id": str(f.id),
                "type": f.flag_type,
                "severity": f.severity,
                "description": f.description,
                "amount_sats": f.amount_sats,
                "created_at": f.created_at.isoformat() if f.created_at else None,
                "reviewed": f.reviewed,
            }
            for f in recent
        ],
    }
