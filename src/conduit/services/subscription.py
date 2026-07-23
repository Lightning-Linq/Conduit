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


def active_listing_cap(sub: ProviderSubscription | None) -> int | None:
    """Active-listing cap for a provider's CURRENT tier.

    Not subscribed (or lapsed) → free cap. Active "starter" → starter cap.
    Any other active tier ("pro" and legacy default rows) → None (unlimited).
    Single source of truth for the registration gate, the lapse sweep, and
    bounded reactivation.
    """
    if not is_subscription_active(sub):
        return settings.free_tier_max_active_skills
    if sub.tier == "starter":
        return settings.subscription_starter_max_active_skills
    return None


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
    cap = active_listing_cap(sub)
    if cap is None or active < cap:
        return True, None

    starter_cap = settings.subscription_starter_max_active_skills
    upgrade = (
        "Upgrade to Pro for unlimited listings"
        if cap >= starter_cap
        else f"Subscribe to Starter ({starter_cap} listings) or Pro (unlimited)"
    )
    return False, (
        f"Your tier allows {cap} active listings; '{provider_name}' has "
        f"{active}. {upgrade} (POST /api/v1/marketplace/subscription)."
    )


async def deactivate_overflow(
    session: AsyncSession, provider_name: str, limit: int
) -> int:
    """Hide the NEWEST active listings beyond `limit` (reversible — renewal
    flips them back). Returns how many were hidden; 0 when within `limit`."""
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


async def reactivate_up_to_cap(
    session: AsyncSession, provider_name: str, cap: int | None
) -> int:
    """Restore hidden listings on renewal, never above the tier cap.

    Unlimited (Pro) reactivates everything; a bounded tier fills only the
    open slots, oldest hidden first. Returns how many were reactivated.
    """
    if cap is None:
        result = await session.execute(
            update(Skill)
            .where(Skill.provider_name == provider_name, Skill.is_active.is_(False))
            .values(is_active=True)
        )
        return result.rowcount or 0

    active = await count_active_skills(session, provider_name)
    slots = cap - active
    if slots <= 0:
        return 0
    result = await session.execute(
        select(Skill.id)
        .where(Skill.provider_name == provider_name, Skill.is_active.is_(False))
        .order_by(Skill.created_at.asc())
        .limit(slots)
    )
    ids = [row[0] for row in result.all()]
    if not ids:
        return 0
    await session.execute(
        update(Skill).where(Skill.id.in_(ids)).values(is_active=True)
    )
    return len(ids)


async def enforce_listing_quotas(session: AsyncSession) -> int:
    """Sweep: for every provider over their tier's cap, hide the overflow.
    Returns total hidden.

    The `count > free_cap` prefilter is a correct superset (the free cap is
    the smallest), so the query is cheap; the per-provider cap decides
    whether anything is actually trimmed. Runs from the background loop in
    main; also safe to call ad hoc.
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
        cap = active_listing_cap(sub)
        if cap is None:  # unlimited — never trimmed
            continue
        total += await deactivate_overflow(session, provider_name, cap)
    if total:
        await session.commit()
    return total
