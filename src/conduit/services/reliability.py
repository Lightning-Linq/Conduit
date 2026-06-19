"""Skill reliability metric (REQ-02 Phase B).

An objective, automatic signal computed from execution outcomes Conduit already
records. It is deliberately kept separate from the subjective 1-5 rating:
reliability answers "does this skill actually deliver, and how fast," not "was
the result any good."

Computed on the fly from SkillExecution rows (no schema change). If this gets
hot at scale, materialize the counters on Skill (REQ-02 Phase C).
"""

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.models.execution import ExecutionStatus, SkillExecution

# Below this many terminal executions we don't publish a rate, so a lucky
# 1-for-1 can't read as "100% reliable."
MIN_RELIABILITY_SAMPLE = 5

# Outcomes that count toward the completion rate: a webhook was attempted after
# payment and resolved to success or failure. PENDING_PAYMENT / PAYMENT_RECEIVED /
# EXECUTING are in-flight. REFUNDED is excluded (N12): a refund is the provider
# making the consumer whole, not a delivery failure, so counting it against
# completion_rate would penalize the right behavior.
_TERMINAL = (
    ExecutionStatus.COMPLETED,
    ExecutionStatus.FAILED,
)


async def get_skill_reliability(session: AsyncSession, skill_id) -> dict:
    """Compute reliability for a skill from its execution history.

    Returns a dict with sample_size, completion_rate (None until the sample
    reaches MIN_RELIABILITY_SAMPLE), distinct_payers, p50_ms, p95_ms, and an
    enough_data flag.
    """
    # One pass for sample size, completed count, and distinct payers.
    payer_ident = func.coalesce(
        SkillExecution.payer_pubkey, SkillExecution.consumer_name
    )
    agg_stmt = select(
        func.count(SkillExecution.id),
        func.coalesce(
            func.sum(
                case((SkillExecution.status == ExecutionStatus.COMPLETED, 1), else_=0)
            ),
            0,
        ),
        func.count(func.distinct(payer_ident)),
    ).where(
        SkillExecution.skill_id == skill_id,
        SkillExecution.status.in_(_TERMINAL),
    )
    sample_size, completed, distinct_payers = (await session.execute(agg_stmt)).one()
    sample_size = sample_size or 0
    completed = completed or 0
    distinct_payers = distinct_payers or 0

    if sample_size < MIN_RELIABILITY_SAMPLE:
        return {
            "sample_size": sample_size,
            "completed": completed,
            "completion_rate": None,
            "distinct_payers": distinct_payers,
            "p50_ms": None,
            "p95_ms": None,
            "enough_data": False,
        }

    p50_ms, p95_ms = await _latency_percentiles(session, skill_id)
    return {
        "sample_size": sample_size,
        "completion_rate": round(completed / sample_size, 3),
        "distinct_payers": distinct_payers,
        "p50_ms": p50_ms,
        "p95_ms": p95_ms,
        "enough_data": True,
    }


async def _latency_percentiles(session: AsyncSession, skill_id):
    """p50/p95 of Conduit-measured latency over completed executions.

    Returns (None, None) when no completed execution has a recorded duration.
    """
    col = SkillExecution.execution_time_ms
    stmt = select(
        func.percentile_cont(0.5).within_group(col.asc()),
        func.percentile_cont(0.95).within_group(col.asc()),
    ).where(
        SkillExecution.skill_id == skill_id,
        SkillExecution.status == ExecutionStatus.COMPLETED,
        col.isnot(None),
    )
    row = (await session.execute(stmt)).one_or_none()
    if not row or row[0] is None:
        return None, None
    return int(row[0]), int(row[1])


def format_reliability_text(rel: dict) -> str:
    """Render the MCP one-line reliability summary."""
    if not rel.get("enough_data"):
        n = rel.get("sample_size", 0)
        completed = rel.get("completed")
        if completed is not None and n:
            # Surface the success/failure split so 0-of-4 reads differently from
            # 4-of-4 instead of both showing a bare run count (S10).
            return f"Reliability: not enough data yet ({completed}/{n} succeeded)"
        return f"Reliability: not enough data yet ({n} run{'s' if n != 1 else ''})"

    line = (
        f"Reliability: {rel['completion_rate'] * 100:.1f}% over "
        f"{rel['sample_size']} runs ({rel['distinct_payers']} payers)"
    )
    if rel.get("p50_ms") is not None:
        line += f", latency {rel['p50_ms']}ms p50 / {rel['p95_ms']}ms p95"
    return line
