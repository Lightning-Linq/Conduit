"""Seller subscription service — listing quotas and lapse enforcement.

Free tier: up to `free_tier_max_active_skills` ACTIVE listings per
provider_name, standard platform fee on every sale. Pro (subscribed,
paid_until in the future): unlimited active listings, same fee.

Enforcement is listing-side only (registration gate + lapse sweep) — it
never touches the payment path. Quotas are real on the LL-hosted node and
advisory on self-hosted ones (provider_name is unauthenticated).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.config import settings
from conduit.models.skill import Skill
from conduit.models.subscription import ProviderSubscription


def is_subscription_active(sub: ProviderSubscription | None) -> bool:
    """Active = paid_until exists and is in the future (UTC)."""
    if sub is None or sub.paid_until is None:
        return False
    paid_until = sub.paid_until
    if paid_until.tzinfo is None:
        paid_until = paid_until.replace(tzinfo=UTC)
    return paid_until > datetime.now(UTC)


async def get_subscription(
    session: AsyncSession, provider_name: str
) -> ProviderSubscription | None:
    result = await session.execute(
        select(ProviderSubscription).where(
            ProviderSubscription.provider_name == provider_name
        )
    )
    return result.scalar_one_or_none()


async def count_active_skills(session: AsyncSession, provider_name: str) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Skill)
        .where(Skill.provider_name == provider_name, Skill.is_active.is_(True))
    )
    return result.scalar() or 0


async def can_register_skill(
    session: AsyncSession, provider_name: str
) -> tuple[bool, str | None]:
    """Gate for BOTH register paths (REST + MCP).

    Returns (allowed, reason). Reason is a user-facing upgrade message when
    blocked. With `subscription_enabled` off, always allowed (no queries).
    """
    if not settings.subscription_enabled:
        return True, None

    active = await count_active_skills(session, provider_name)
    if active < settings.free_tier_max_active_skills:
        return True, None

    sub = await get_subscription(session, provider_name)
    if is_subscription_active(sub):
        return True, None

    return False, (
        f"Free tier allows {settings.free_tier_max_active_skills} active "
        f"listings; '{provider_name}' has {active}. Subscribe to Pro for "
        f"unlimited listings (POST /api/v1/marketplace/subscription)."
    )


async def deactivate_overflow(session: AsyncSession, provider_name: str) -> int:
    """Hide the NEWEST active listings beyond the free quota (reversible —
    renewal flips them back). Returns how many were hidden."""
    limit = settings.free_tier_max_active_skills
    result = await session.execute(
        select(Skill.id)
        .where(Skill.provider_name == provider_name, Skill.is_active.is_(True))
        .order_by(Skill.created_at.asc())
        .offset(limit)
    )
    overflow_ids = [row[0] for row in result.all()]
    if not overflow_ids:
        return 0
    await session.execute(
        update(Skill).where(Skill.id.in_(overflow_ids)).values(is_active=False)
    )
    return len(overflow_ids)


async def enforce_listing_quotas(session: AsyncSession) -> int:
    """Sweep: for every provider over the free quota WITHOUT an active
    subscription, deactivate their overflow. Returns total hidden.

    Runs from the background loop in main; also safe to call ad hoc.
    """
    if not settings.subscription_enabled:
        return 0

    over = await session.execute(
        select(Skill.provider_name)
        .where(Skill.is_active.is_(True))
        .group_by(Skill.provider_name)
        .having(func.count() > settings.free_tier_max_active_skills)
    )
    total = 0
    for (provider_name,) in over.all():
        sub = await get_subscription(session, provider_name)
        if not is_subscription_active(sub):
            total += await deactivate_overflow(session, provider_name)
    if total:
        await session.commit()
    return total
