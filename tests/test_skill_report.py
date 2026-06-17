"""Unit tests for REQ-09 consumer skill reports.

The DB session is mocked, so no Postgres is needed.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from conduit.services.skill_report import (
    REPORT_SEVERITY,
    create_skill_report,
    normalize_category,
)


def _skill(name="demo", provider="prov", sid="11111111-1111-1111-1111-111111111111"):
    return SimpleNamespace(id=sid, name=name, provider_name=provider)


def _session():
    s = AsyncMock()
    s.add = MagicMock()  # Session.add is sync in real SQLAlchemy
    s.commit = AsyncMock()
    return s


# =============================================================================
# normalize_category
# =============================================================================


def test_normalize_category_known_and_unknown():
    assert normalize_category("UNSAFE") == "unsafe"
    assert normalize_category(" scam ") == "scam"
    assert normalize_category("nonsense") == "other"
    assert normalize_category(None) == "other"


# =============================================================================
# create_skill_report
# =============================================================================


async def test_create_report_maps_severity_and_fields():
    session = _session()
    flag = await create_skill_report(
        session,
        skill=_skill(),
        reason="it stole my sats",
        category="scam",
        reporter_name="alice",
    )
    assert flag.flag_type == "consumer_report"
    assert flag.severity == "high"  # scam -> high
    assert flag.skill_id == "11111111-1111-1111-1111-111111111111"
    assert flag.provider_name == "prov"
    assert flag.consumer_name == "alice"
    assert "scam" in flag.description
    assert "demo" in flag.description
    assert "alice" in flag.description
    session.add.assert_called_once_with(flag)
    session.commit.assert_awaited_once()


async def test_create_report_unknown_category_defaults_to_other_medium():
    session = _session()
    flag = await create_skill_report(
        session, skill=_skill(), reason="x", category="weird"
    )
    assert flag.severity == REPORT_SEVERITY["other"]  # medium
    assert "(other)" in flag.description


async def test_create_report_strips_control_chars():
    session = _session()
    flag = await create_skill_report(
        session, skill=_skill(), reason="bad\x00\x1b[31mstuff"
    )
    assert "\x00" not in flag.description
    assert "\x1b" not in flag.description
    assert "badstuff" in flag.description


async def test_create_report_caps_reason_length():
    session = _session()
    flag = await create_skill_report(session, skill=_skill(), reason="A" * 2000)
    assert flag.description.count("A") == 1000  # reason truncated to 1000


async def test_create_report_anonymous_when_no_reporter():
    session = _session()
    flag = await create_skill_report(session, skill=_skill(), reason="broken")
    assert flag.consumer_name is None
    assert "anonymous" in flag.description
