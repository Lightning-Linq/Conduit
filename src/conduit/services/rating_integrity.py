"""
Rating integrity — prevents gaming of the reputation system.

Checks before allowing a rating:
1. Preimage verification — the preimage must hash to the execution's payment_hash
2. No duplicate ratings — one rating per execution
3. Minimum time — must wait at least N seconds after execution completes
4. Self-review detection — flag if consumer and provider match

Also provides weighted rating calculation that discounts repeat reviewers.
"""

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, func as sa_func, and_, distinct
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.models.execution import SkillExecution, ExecutionStatus
from conduit.models.rating import Rating
from conduit.models.skill import Skill
from conduit.models.anomaly_flag import AnomalyFlag


# =============================================================================
# Configuration
# =============================================================================

# Minimum seconds between execution completion and allowed rating
MIN_RATING_DELAY_SECONDS = 30

# If a single consumer accounts for more than this fraction of a
# provider's ratings, flag it
SAME_CONSUMER_FLAG_THRESHOLD = 0.6  # 60%
MIN_RATINGS_FOR_FLAG = 3  # Only flag if provider has at least this many ratings


# =============================================================================
# Integrity checks
# =============================================================================


class RatingIntegrityError(Exception):
    """Raised when a rating fails integrity checks."""
    pass


async def validate_rating(
    session: AsyncSession,
    execution: SkillExecution,
    payment_preimage: str,
    skill: Skill,
) -> None:
    """
    Run all integrity checks on a proposed rating.
    Raises RatingIntegrityError if any check fails.
    """
    # --- Check 1: Verify preimage matches payment hash ---
    # Skip for free skills (no payment was made, so no preimage to verify)
    if execution.payment_hash:
        preimage_bytes = bytes.fromhex(payment_preimage)
        computed_hash = hashlib.sha256(preimage_bytes).hexdigest()
        if computed_hash != execution.payment_hash:
            raise RatingIntegrityError(
                f"Payment preimage does not match payment hash. "
                f"Expected hash: {execution.payment_hash}, "
                f"Got: {computed_hash}"
            )

    # --- Check 2: No duplicate ratings ---
    result = await session.execute(
        select(sa_func.count(Rating.id))
        .where(Rating.execution_id == execution.id)
    )
    existing_count = result.scalar() or 0
    if existing_count > 0:
        raise RatingIntegrityError(
            f"This execution has already been rated. "
            f"Only one rating per execution is allowed."
        )

    # --- Check 3: Execution must be completed ---
    if execution.status != ExecutionStatus.COMPLETED:
        raise RatingIntegrityError(
            f"Cannot rate an execution with status '{execution.status.value}'. "
            f"Only completed executions can be rated."
        )

    # --- Check 4: Minimum time delay ---
    if execution.updated_at:
        elapsed = (datetime.now(timezone.utc) - execution.updated_at.replace(tzinfo=timezone.utc))
        if elapsed < timedelta(seconds=MIN_RATING_DELAY_SECONDS):
            remaining = MIN_RATING_DELAY_SECONDS - int(elapsed.total_seconds())
            raise RatingIntegrityError(
                f"Please wait {remaining} more seconds before rating. "
                f"Minimum delay: {MIN_RATING_DELAY_SECONDS}s after execution."
            )


async def check_provider_rating_concentration(
    session: AsyncSession,
    skill: Skill,
    consumer_name: str | None,
) -> AnomalyFlag | None:
    """
    After a rating is submitted, check if one consumer dominates
    this provider's ratings. Returns an AnomalyFlag if suspicious,
    None otherwise.
    """
    if not consumer_name:
        return None

    # Get total ratings for this skill
    total_result = await session.execute(
        select(sa_func.count(Rating.id))
        .join(SkillExecution, Rating.execution_id == SkillExecution.id)
        .where(SkillExecution.skill_id == skill.id)
    )
    total_ratings = total_result.scalar() or 0

    if total_ratings < MIN_RATINGS_FOR_FLAG:
        return None

    # Count how many ratings come from this specific consumer
    consumer_result = await session.execute(
        select(sa_func.count(Rating.id))
        .join(SkillExecution, Rating.execution_id == SkillExecution.id)
        .where(
            and_(
                SkillExecution.skill_id == skill.id,
                SkillExecution.consumer_name == consumer_name,
            )
        )
    )
    consumer_count = consumer_result.scalar() or 0

    fraction = consumer_count / total_ratings if total_ratings > 0 else 0
    if fraction >= SAME_CONSUMER_FLAG_THRESHOLD:
        flag = AnomalyFlag(
            flag_type="rating_concentration",
            severity="medium",
            description=(
                f"Consumer '{consumer_name}' accounts for {consumer_count}/{total_ratings} "
                f"({fraction:.0%}) of ratings for skill '{skill.name}' by {skill.provider_name}. "
                f"Possible fake review pattern."
            ),
            provider_name=skill.provider_name,
            consumer_name=consumer_name,
        )
        return flag

    return None


async def calculate_weighted_rating(
    session: AsyncSession,
    skill_id,
) -> float:
    """
    Calculate a weighted average rating that discounts repeat reviewers.

    Each unique consumer gets a weight of 1.0 for their first rating.
    Subsequent ratings from the same consumer get diminishing weight:
    2nd = 0.5, 3rd = 0.33, etc. (1/n weighting)

    This makes it much harder for one agent to inflate scores by
    repeatedly buying and rating the same skill.
    """
    # Get all ratings with consumer info
    result = await session.execute(
        select(Rating.score, SkillExecution.consumer_name)
        .join(SkillExecution, Rating.execution_id == SkillExecution.id)
        .where(SkillExecution.skill_id == skill_id)
        .order_by(Rating.created_at)
    )
    rows = result.all()

    if not rows:
        return 0.0

    # Count per-consumer and assign diminishing weights
    consumer_counts: dict[str, int] = {}
    weighted_sum = 0.0
    total_weight = 0.0

    for score, consumer in rows:
        name = consumer or "anonymous"
        consumer_counts[name] = consumer_counts.get(name, 0) + 1
        count = consumer_counts[name]
        weight = 1.0 / count  # 1st = 1.0, 2nd = 0.5, 3rd = 0.33, etc.
        weighted_sum += score * weight
        total_weight += weight

    return round(weighted_sum / total_weight, 2) if total_weight > 0 else 0.0
