"""Consumer skill reports (REQ-09).

Lets a consumer flag a skill as unsafe, broken, a scam, etc. Stored as an
AnomalyFlag so reports flow into the existing moderation tooling
(get_anomaly_report) and audit trail instead of a parallel system. Reporting is
identity/payment-free on purpose: anyone can warn about a listing, including
before they buy. Reports are advisory and never auto-delist a skill.
"""

import re

from sqlalchemy.ext.asyncio import AsyncSession

from conduit.models.anomaly_flag import AnomalyFlag
from conduit.models.skill import Skill

# Report category -> flag severity.
REPORT_SEVERITY: dict[str, str] = {
    "unsafe": "high",
    "scam": "high",
    "broken": "medium",
    "wrong_result": "medium",
    "spam": "low",
    "other": "medium",
}
_DEFAULT_CATEGORY = "other"

# Strip ANSI / C0 control bytes from consumer-supplied text before it lands in
# logs and MCP stdio output (the transport is line-framed; escapes corrupt it).
# The full CSI-escape alternative is tried first so the whole sequence is removed
# rather than just its ESC byte (which would leave a visible "[31m" behind).
_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|[\x00-\x08\x0b-\x1f\x7f\x9b]")


def _clean(text: str | None, limit: int) -> str:
    return _CONTROL.sub("", text or "").strip()[:limit]


def normalize_category(category: str | None) -> str:
    """Coerce a free-form category to a known one (default 'other')."""
    cat = (category or _DEFAULT_CATEGORY).strip().lower()
    return cat if cat in REPORT_SEVERITY else _DEFAULT_CATEGORY


async def create_skill_report(
    session: AsyncSession,
    *,
    skill: Skill,
    reason: str,
    category: str | None = None,
    execution_id: str | None = None,
    reporter_name: str | None = None,
) -> AnomalyFlag:
    """Persist a consumer report as an advisory AnomalyFlag and return it."""
    cat = normalize_category(category)
    reason_clean = _clean(reason, 1000)
    reporter_clean = _clean(reporter_name, 255) or None

    flag = AnomalyFlag(
        flag_type="consumer_report",
        severity=REPORT_SEVERITY[cat],
        description=(
            f"Consumer report ({cat}) for skill '{skill.name}' by "
            f"{reporter_clean or 'anonymous'}: {reason_clean or '(no reason given)'}"
        ),
        skill_id=str(skill.id),
        execution_id=execution_id,
        consumer_name=reporter_clean,
        provider_name=skill.provider_name,
    )
    session.add(flag)
    await session.commit()
    return flag
