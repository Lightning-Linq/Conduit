"""Layer C e2e: the core marketplace tables against a REAL Postgres.

Complements test_federation_e2e (which covers the federation cache). These run the
real service SQL over the tables that silently drifted in the audit, the
skill_executions federation columns and anomaly_flags.skill_id, so a schema
regression fails CI instead of slipping through. Opt-in via `-m e2e`; uses the
shared e2e_session fixture from conftest. Each test uses unique ids, so no
truncation is needed.
"""

import uuid

import pytest
from sqlalchemy import select

from conduit.core.config import settings
from conduit.models.execution import ExecutionStatus, SkillExecution
from conduit.models.rating import Rating
from conduit.models.skill import Skill
from conduit.services.rating_prompt import build_rating_prompt
from conduit.services.reliability import get_skill_reliability
from conduit.services.skill_report import create_skill_report

pytestmark = pytest.mark.e2e


async def _make_skill(session, provider: str) -> Skill:
    skill = Skill(
        provider_name=provider,
        name="demo",
        description="d",
        category="general",
        price_sats=100,
        endpoint_url="https://provider.test/skills/demo",
    )
    session.add(skill)
    await session.commit()
    return skill


async def test_reliability_real_sql(e2e_session):
    """get_skill_reliability over real rows: completion rate, distinct payers, p50."""
    skill = await _make_skill(e2e_session, provider=f"prov-{uuid.uuid4()}")
    for i in range(5):
        e2e_session.add(
            SkillExecution(
                skill_id=skill.id,
                amount_sats=100,
                status=ExecutionStatus.COMPLETED,
                payer_pubkey=f"{i:064x}",
                execution_time_ms=100 + i,
            )
        )
    e2e_session.add(
        SkillExecution(
            skill_id=skill.id,
            amount_sats=100,
            status=ExecutionStatus.FAILED,
            payer_pubkey=f"{9:064x}",
        )
    )
    await e2e_session.commit()

    rel = await get_skill_reliability(e2e_session, skill.id)
    assert rel["enough_data"] is True
    assert rel["sample_size"] == 6
    assert rel["completion_rate"] == round(5 / 6, 3)
    assert rel["distinct_payers"] == 6
    assert rel["p50_ms"] is not None


async def test_skill_report_real_sql(e2e_session):
    """create_skill_report persists an AnomalyFlag carrying anomaly_flags.skill_id."""
    skill = await _make_skill(e2e_session, provider=f"prov-{uuid.uuid4()}")
    flag = await create_skill_report(
        e2e_session, skill=skill, reason="looks like a scam", category="scam"
    )
    assert flag.flag_type == "consumer_report"
    assert flag.skill_id == str(skill.id)
    assert flag.severity == "high"


async def test_rating_prompt_and_rating_real_sql(e2e_session, monkeypatch):
    """First-time-provider prompt query + a ratings table round-trip."""
    monkeypatch.setattr(settings, "rating_prompt_policy", "first_time_provider")
    skill = await _make_skill(e2e_session, provider=f"prov-{uuid.uuid4()}")
    execution = SkillExecution(
        skill_id=skill.id,
        amount_sats=100,
        status=ExecutionStatus.COMPLETED,
        payer_pubkey=f"{1:064x}",
    )
    e2e_session.add(execution)
    await e2e_session.commit()

    payload = await build_rating_prompt(e2e_session, skill, execution)
    assert payload["should_prompt_rating"] is True  # first execution for this provider

    e2e_session.add(Rating(execution_id=execution.id, score=5, rater_name="c"))
    await e2e_session.commit()
    stored = (
        await e2e_session.execute(
            select(Rating).where(Rating.execution_id == execution.id)
        )
    ).scalar_one()
    assert stored.score == 5
