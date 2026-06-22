"""e2e tests for the anomaly detector (real Postgres SQL).

The detector opens its OWN session via `async_session_factory()` and runs windowed
aggregate SQL, so these run against the dedicated `conduit_e2e` database: we
monkeypatch the detector's factory onto the `e2e_session_factory` fixture, seed rows
through `e2e_session`, and assert the flags the real SQL produces. Marked `e2e`
(deselected by default; run with `-m e2e`).

These are coverage/regression tests for already-working code — each locks in current
behavior and asserts a security invariant. Note `test_volume_spike_cold_start_*`
documents a real quirk (a first-ever payment trips the spike), not a desired outcome.

The structuring and volume-spike queries are GLOBAL (not name-scoped), so those tests
TRUNCATE `spending_logs` first for determinism; name/skill-scoped checks use unique ids.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from conduit.models.anomaly_flag import AnomalyFlag
from conduit.models.execution import SkillExecution
from conduit.models.rating import Rating  # noqa: F401 - registers mapper for SkillExecution.ratings
from conduit.models.skill import Skill
from conduit.models.spending_log import SpendingLog

pytestmark = pytest.mark.e2e


# ── Fixtures & helpers ───────────────────────────────────────────────────────


@pytest.fixture
def detector(monkeypatch, e2e_session_factory):
    """The anomaly_detector module with its async_session_factory pointed at conduit_e2e."""
    import conduit.services.anomaly_detector as ad

    monkeypatch.setattr(ad, "async_session_factory", e2e_session_factory)
    return ad


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


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


def _exec(skill_id, consumer: str, *, created_at: datetime, amount: int = 100) -> SkillExecution:
    return SkillExecution(
        skill_id=skill_id, consumer_name=consumer, amount_sats=amount, created_at=created_at
    )


def _spend(
    amount: int, *, status: str = "allowed", created_at: datetime, tool: str = "pay_invoice"
) -> SpendingLog:
    return SpendingLog(tool_name=tool, amount_sats=amount, status=status, created_at=created_at)


async def _truncate(session, *tables: str) -> None:
    await session.execute(text("TRUNCATE " + ", ".join(tables)))
    await session.commit()


async def _flags(session, **filters) -> list[AnomalyFlag]:
    stmt = select(AnomalyFlag)
    for key, val in filters.items():
        stmt = stmt.where(getattr(AnomalyFlag, key) == val)
    return list((await session.execute(stmt)).scalars().all())


# ── Self-payment (no DB read) ────────────────────────────────────────────────


async def test_self_payment_flags_matching_names(detector, e2e_session):
    name = _uniq("agent")
    flags = await detector.check_for_anomalies(
        consumer_name=name, provider_name=name, amount_sats=0
    )
    assert any(f.flag_type == "self_payment" and f.severity == "high" for f in flags)
    assert len(await _flags(e2e_session, consumer_name=name, flag_type="self_payment")) == 1


async def test_self_payment_is_case_insensitive(detector, e2e_session):
    base = _uniq("Agent")
    flags = await detector.check_for_anomalies(
        consumer_name=base.upper(), provider_name=base.lower(), amount_sats=0
    )
    assert any(f.flag_type == "self_payment" for f in flags)


async def test_self_payment_different_names_no_flag(detector, e2e_session):
    flags = await detector.check_for_anomalies(
        consumer_name=_uniq("a"), provider_name=_uniq("b"), amount_sats=0
    )
    assert flags == []


async def test_self_payment_requires_both_names(detector, e2e_session):
    flags = await detector.check_for_anomalies(
        consumer_name=_uniq("a"), provider_name=None, amount_sats=0
    )
    assert flags == []


# ── Rapid repeat (name + skill scoped, 30-min window) ────────────────────────


async def test_rapid_repeat_flags_at_threshold(detector, e2e_session):
    consumer = _uniq("consumer")
    skill = await _make_skill(e2e_session, _uniq("prov"))
    now = datetime.now(UTC)
    for i in range(3):  # RAPID_REPEAT_MAX == 3
        e2e_session.add(_exec(skill.id, consumer, created_at=now - timedelta(minutes=i + 1)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(
        consumer_name=consumer, skill_id=str(skill.id), amount_sats=0
    )
    assert any(f.flag_type == "rapid_repeat" and f.severity == "medium" for f in flags)


async def test_rapid_repeat_below_threshold_no_flag(detector, e2e_session):
    consumer = _uniq("consumer")
    skill = await _make_skill(e2e_session, _uniq("prov"))
    now = datetime.now(UTC)
    for i in range(2):  # below 3
        e2e_session.add(_exec(skill.id, consumer, created_at=now - timedelta(minutes=i + 1)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(
        consumer_name=consumer, skill_id=str(skill.id), amount_sats=0
    )
    assert not any(f.flag_type == "rapid_repeat" for f in flags)


async def test_rapid_repeat_excludes_old_executions(detector, e2e_session):
    consumer = _uniq("consumer")
    skill = await _make_skill(e2e_session, _uniq("prov"))
    now = datetime.now(UTC)
    for i in range(3):  # 3, but all older than the 30-min window
        e2e_session.add(_exec(skill.id, consumer, created_at=now - timedelta(minutes=40 + i)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(
        consumer_name=consumer, skill_id=str(skill.id), amount_sats=0
    )
    assert not any(f.flag_type == "rapid_repeat" for f in flags)


async def test_rapid_repeat_skipped_without_skill_id(detector, e2e_session):
    flags = await detector.check_for_anomalies(
        consumer_name=_uniq("consumer"), skill_id=None, amount_sats=0
    )
    assert not any(f.flag_type == "rapid_repeat" for f in flags)


# ── Structuring (GLOBAL query — truncate spending_logs; 80-100% band, 1-hr window) ──


async def test_structuring_flags_clustered_near_limit(detector, e2e_session, monkeypatch):
    monkeypatch.setattr(detector.settings, "spending_limit_per_payment_sats", 10_000)
    await _truncate(e2e_session, "spending_logs")
    now = datetime.now(UTC)
    for _ in range(3):  # STRUCTURING_COUNT == 3, 9_000 is in the 8_000-10_000 band
        e2e_session.add(_spend(9_000, created_at=now - timedelta(minutes=5)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(consumer_name=_uniq("c"), amount_sats=9_000)
    assert any(f.flag_type == "structuring" and f.severity == "high" for f in flags)


async def test_structuring_below_count_no_flag(detector, e2e_session, monkeypatch):
    monkeypatch.setattr(detector.settings, "spending_limit_per_payment_sats", 10_000)
    await _truncate(e2e_session, "spending_logs")
    now = datetime.now(UTC)
    for _ in range(2):  # only 2 in band
        e2e_session.add(_spend(9_000, created_at=now - timedelta(minutes=5)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(consumer_name=_uniq("c"), amount_sats=9_000)
    assert not any(f.flag_type == "structuring" for f in flags)


async def test_structuring_ignores_payments_below_band(detector, e2e_session, monkeypatch):
    monkeypatch.setattr(detector.settings, "spending_limit_per_payment_sats", 10_000)
    await _truncate(e2e_session, "spending_logs")
    now = datetime.now(UTC)
    for _ in range(3):  # 5_000 < 8_000 (below the 80% floor)
        e2e_session.add(_spend(5_000, created_at=now - timedelta(minutes=5)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(consumer_name=_uniq("c"), amount_sats=5_000)
    assert not any(f.flag_type == "structuring" for f in flags)


async def test_structuring_ignores_non_allowed_status(detector, e2e_session, monkeypatch):
    monkeypatch.setattr(detector.settings, "spending_limit_per_payment_sats", 10_000)
    await _truncate(e2e_session, "spending_logs")
    now = datetime.now(UTC)
    for _ in range(3):  # in band, but blocked (not "allowed")
        e2e_session.add(_spend(9_000, status="blocked", created_at=now - timedelta(minutes=5)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(consumer_name=_uniq("c"), amount_sats=9_000)
    assert not any(f.flag_type == "structuring" for f in flags)


async def test_structuring_excludes_old_payments(detector, e2e_session, monkeypatch):
    monkeypatch.setattr(detector.settings, "spending_limit_per_payment_sats", 10_000)
    await _truncate(e2e_session, "spending_logs")
    now = datetime.now(UTC)
    for _ in range(3):  # in band, but older than the 1-hour window
        e2e_session.add(_spend(9_000, created_at=now - timedelta(hours=2)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(consumer_name=_uniq("c"), amount_sats=9_000)
    assert not any(f.flag_type == "structuring" for f in flags)


# ── Volume spike (GLOBAL query — truncate spending_logs; >5x 7-day hourly avg) ──


async def test_volume_spike_flags_on_surge(detector, e2e_session):
    await _truncate(e2e_session, "spending_logs")
    now = datetime.now(UTC)
    # Steady history 2 days ago dominates the 7-day average; a big last-hour spend spikes.
    e2e_session.add(_spend(2_000, created_at=now - timedelta(days=2)))
    e2e_session.add(_spend(50_000, created_at=now - timedelta(minutes=10)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(consumer_name=_uniq("c"), amount_sats=50_000)
    assert any(f.flag_type == "volume_spike" and f.severity == "medium" for f in flags)


async def test_volume_spike_under_threshold_no_flag(detector, e2e_session):
    await _truncate(e2e_session, "spending_logs")
    now = datetime.now(UTC)
    # Large history → high hourly average; a modest last-hour spend stays under 5x.
    e2e_session.add(_spend(100_000, created_at=now - timedelta(days=2)))
    e2e_session.add(_spend(400, created_at=now - timedelta(minutes=10)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(consumer_name=_uniq("c"), amount_sats=400)
    assert not any(f.flag_type == "volume_spike" for f in flags)


async def test_volume_spike_cold_start_quirk(detector, e2e_session):
    """Documents a real limitation: with no prior history, the 7-day average divides by
    a fixed 168h, so the first-ever payment reads as a spike. Advisory-only, but noisy.
    Captured as a regression marker — if the detector learns a min-history guard, update this."""
    await _truncate(e2e_session, "spending_logs")
    now = datetime.now(UTC)
    e2e_session.add(_spend(1_000, created_at=now - timedelta(minutes=5)))
    await e2e_session.commit()
    flags = await detector.check_for_anomalies(consumer_name=_uniq("c"), amount_sats=1_000)
    assert any(f.flag_type == "volume_spike" for f in flags)


# ── Aggregate behaviors ──────────────────────────────────────────────────────


async def test_clean_transaction_flags_nothing(detector, e2e_session):
    name = _uniq("clean")
    flags = await detector.check_for_anomalies(
        consumer_name=name, provider_name=_uniq("other"), skill_id=None, amount_sats=0
    )
    assert flags == []
    assert await _flags(e2e_session, consumer_name=name) == []


async def test_multiple_flags_persist_in_one_call(detector, e2e_session, monkeypatch):
    monkeypatch.setattr(detector.settings, "spending_limit_per_payment_sats", 10_000)
    await _truncate(e2e_session, "spending_logs")
    now = datetime.now(UTC)
    for _ in range(3):
        e2e_session.add(_spend(9_000, created_at=now - timedelta(minutes=5)))
    await e2e_session.commit()
    name = _uniq("self")
    ph = uuid.uuid4().hex
    flags = await detector.check_for_anomalies(
        consumer_name=name, provider_name=name, amount_sats=9_000, payment_hash=ph
    )
    types = {f.flag_type for f in flags}
    assert {"self_payment", "structuring"} <= types
    # every fired flag was persisted under this call's payment_hash
    assert len(await _flags(e2e_session, payment_hash=ph)) == len(flags) >= 2


async def test_get_anomaly_summary_aggregates(detector, e2e_session):
    await _truncate(e2e_session, "anomaly_flags")
    e2e_session.add_all([
        AnomalyFlag(flag_type="self_payment", severity="high", description="d", reviewed=False),
        AnomalyFlag(flag_type="structuring", severity="high", description="d", reviewed=True),
        AnomalyFlag(flag_type="volume_spike", severity="medium", description="d", reviewed=False),
    ])
    await e2e_session.commit()
    summary = await detector.get_anomaly_summary()
    assert summary["total_flags"] == 3
    assert summary["unreviewed"] == 2
    assert summary["by_severity"]["high"] == 2
    assert summary["by_severity"]["medium"] == 1
    assert summary["by_type"]["self_payment"] == 1
    assert len(summary["recent"]) == 3
