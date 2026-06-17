"""Rating-prompt policy (REQ-02 Phase A).

Decides whether a completed execution should *invite* the consumer/agent to
leave a 1-5 rating. Ratings are always explicit and payment-bound; nothing here
auto-generates a score. This only controls how often we nudge, via a
configurable cadence policy, so integrators and the hosted tier can tune it
without forking core.

Reliability (the automatic, objective signal) is REQ-02 Phase B and lives
elsewhere; this module is purely about prompting for the subjective rating.
"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.config import settings
from conduit.models.execution import ExecutionStatus, SkillExecution
from conduit.models.skill import Skill

VALID_POLICIES = ("always", "first_time_provider", "above_threshold", "never")


def should_prompt_rating(
    amount_sats: int,
    is_first_time_for_provider: bool,
    *,
    policy: str | None = None,
    threshold_sats: int | None = None,
) -> bool:
    """Pure policy decision: should we nudge for a rating?

    Falls back to the configured settings when policy/threshold are omitted.
    An unrecognized policy returns False (do not prompt) so a typo can never
    spam users.
    """
    policy = policy if policy is not None else settings.rating_prompt_policy
    threshold = (
        threshold_sats
        if threshold_sats is not None
        else settings.rating_prompt_sat_threshold
    )

    if policy == "always":
        return True
    if policy == "never":
        return False
    if policy == "above_threshold":
        return amount_sats >= threshold
    if policy == "first_time_provider":
        return is_first_time_for_provider
    return False


async def _is_first_provider_execution(
    session: AsyncSession,
    skill: Skill,
    execution: SkillExecution,
) -> bool:
    """True if this consumer has no *other* completed execution from this provider.

    Consumer identity is the payer pubkey when present, otherwise the (free-form)
    consumer_name. In single-tenant setups consumer_name often defaults to
    "anonymous", so this collapses to "once per provider" there (see the H12 note
    in rating_integrity.py). Pass a distinct payer_pubkey / consumer_name for true
    per-consumer behavior.
    """
    if execution.consumer_pubkey:
        consumer_col = SkillExecution.consumer_pubkey
        consumer_val = execution.consumer_pubkey
    else:
        consumer_col = SkillExecution.consumer_name
        consumer_val = execution.consumer_name

    stmt = (
        select(func.count(SkillExecution.id))
        .join(Skill, SkillExecution.skill_id == Skill.id)
        .where(
            Skill.provider_name == skill.provider_name,
            SkillExecution.id != execution.id,
            SkillExecution.status == ExecutionStatus.COMPLETED,
            consumer_col == consumer_val,
        )
    )
    prior = (await session.execute(stmt)).scalar() or 0
    return prior == 0


async def build_rating_prompt(
    session: AsyncSession,
    skill: Skill,
    execution: SkillExecution,
) -> dict:
    """Build the rating-affordance payload for a completed execution.

    Returns ``{execution_id, should_prompt_rating, rating_policy}``. The
    execution_id is always included so a rating is *possible*; the boolean only
    governs whether we actively nudge.
    """
    policy = settings.rating_prompt_policy

    # Only the first_time_provider policy needs a DB lookup; skip it otherwise.
    if policy == "first_time_provider":
        is_first = await _is_first_provider_execution(session, skill, execution)
    else:
        is_first = False

    return {
        "execution_id": str(execution.id),
        "should_prompt_rating": should_prompt_rating(execution.amount_sats, is_first),
        "rating_policy": policy,
    }


def format_rating_prompt_text(payload: dict) -> str:
    """Render the MCP text affordance.

    Always surfaces the execution_id (so the agent *can* rate); adds an explicit
    nudge only when the policy says to ask. The preimage is not echoed here: the
    caller already passed it to confirm.
    """
    eid = payload["execution_id"]
    lines = [f"\n\nExecution ID: {eid}"]
    if payload.get("should_prompt_rating"):
        lines.append(
            "Rate this skill: if it was useful, ask the user for a 1-5 score, then "
            f'call submit_rating with execution_id "{eid}", the score, and the '
            "payment preimage you just used. Payment-bound ratings are what build "
            "a provider's reputation, so do not invent a score."
        )
    return "\n".join(lines)
