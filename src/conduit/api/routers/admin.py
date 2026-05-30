"""Admin endpoints - database management and maintenance.

2 endpoints:
  DELETE /api/v1/admin/reset-demo    Wipe all demo data (skills, executions, ratings, anomaly flags)
  GET    /api/v1/admin/stats         Database row counts
"""

from fastapi import APIRouter, Depends
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.api.deps import verify_api_key, get_session
from conduit.models.skill import Skill
from conduit.models.execution import SkillExecution
from conduit.models.rating import Rating
from conduit.models.anomaly_flag import AnomalyFlag

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("/stats")
async def get_stats(session: AsyncSession = Depends(get_session)):
    """Get row counts for all marketplace tables."""
    skills = (await session.execute(select(func.count(Skill.id)))).scalar() or 0
    executions = (await session.execute(select(func.count(SkillExecution.id)))).scalar() or 0
    ratings = (await session.execute(select(func.count(Rating.id)))).scalar() or 0
    flags = (await session.execute(select(func.count(AnomalyFlag.id)))).scalar() or 0

    return {
        "skills": skills,
        "executions": executions,
        "ratings": ratings,
        "anomaly_flags": flags,
        "total": skills + executions + ratings + flags,
    }


@router.delete("/reset-demo")
async def reset_demo(session: AsyncSession = Depends(get_session)):
    """
    Wipe ALL marketplace data: ratings, executions, skills, and anomaly flags.

    Deletes in FK-safe order. This is irreversible - intended for clearing
    demo/test data, not for production use.
    """
    # Delete in FK-safe order: ratings -> executions -> skills, then flags
    r_count = (await session.execute(delete(Rating))).rowcount
    e_count = (await session.execute(delete(SkillExecution))).rowcount
    s_count = (await session.execute(delete(Skill))).rowcount
    f_count = (await session.execute(delete(AnomalyFlag))).rowcount

    await session.commit()

    return {
        "reset": True,
        "deleted": {
            "ratings": r_count,
            "executions": e_count,
            "skills": s_count,
            "anomaly_flags": f_count,
            "total": r_count + e_count + s_count + f_count,
        },
    }
