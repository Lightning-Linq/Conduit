"""Unit tests for the REQ-02 Phase B reliability metric.

The two SQL reads are mocked, so no Postgres is needed; the percentile SQL
itself is Postgres-specific and exercised end to end elsewhere.
"""

from unittest.mock import AsyncMock, MagicMock

from conduit.services.reliability import (
    MIN_RELIABILITY_SAMPLE,
    format_reliability_text,
    get_skill_reliability,
)


def _agg_result(sample, completed, payers):
    """First query: (sample_size, completed, distinct_payers)."""
    r = MagicMock()
    r.one.return_value = (sample, completed, payers)
    return r


def _pct_result(p50, p95):
    """Second query: (p50_ms, p95_ms)."""
    r = MagicMock()
    r.one_or_none.return_value = (p50, p95)
    return r


# =============================================================================
# get_skill_reliability
# =============================================================================


async def test_below_min_sample_hides_rate_and_skips_latency():
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_agg_result(MIN_RELIABILITY_SAMPLE - 1, 2, 2))

    rel = await get_skill_reliability(session, "skill-1")

    assert rel["enough_data"] is False
    assert rel["completion_rate"] is None
    assert rel["sample_size"] == MIN_RELIABILITY_SAMPLE - 1
    assert rel["distinct_payers"] == 2
    assert rel["p50_ms"] is None
    # No second (percentile) query when there isn't enough data.
    session.execute.assert_awaited_once()


async def test_computes_rate_and_latency_above_sample():
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[_agg_result(10, 9, 4), _pct_result(120.0, 800.0)]
    )

    rel = await get_skill_reliability(session, "skill-1")

    assert rel["enough_data"] is True
    assert rel["completion_rate"] == 0.9
    assert rel["sample_size"] == 10
    assert rel["distinct_payers"] == 4
    assert rel["p50_ms"] == 120
    assert rel["p95_ms"] == 800
    assert session.execute.await_count == 2


async def test_completion_rate_is_rounded():
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[_agg_result(12, 7, 6), _pct_result(50.0, 90.0)]
    )

    rel = await get_skill_reliability(session, "skill-1")

    assert rel["completion_rate"] == round(7 / 12, 3)  # 0.583


async def test_missing_latency_returns_none():
    session = AsyncMock()
    session.execute = AsyncMock(
        side_effect=[_agg_result(6, 5, 3), _pct_result(None, None)]
    )

    rel = await get_skill_reliability(session, "skill-1")

    assert rel["enough_data"] is True
    assert rel["p50_ms"] is None
    assert rel["p95_ms"] is None


async def test_null_aggregates_coerced_to_zero():
    # An empty history can come back as (0, None, 0); sum() is NULL -> 0.
    session = AsyncMock()
    session.execute = AsyncMock(return_value=_agg_result(0, None, 0))

    rel = await get_skill_reliability(session, "skill-1")

    assert rel["sample_size"] == 0
    assert rel["enough_data"] is False


# =============================================================================
# format_reliability_text
# =============================================================================


def test_format_not_enough_data_pluralizes():
    assert "3 runs" in format_reliability_text({"enough_data": False, "sample_size": 3})
    txt = format_reliability_text({"enough_data": False, "sample_size": 1})
    assert "1 run" in txt and "1 runs" not in txt


def test_format_not_enough_data_surfaces_failures():
    # 0 of 4 must read differently from 4 of 4 (S10).
    bad = format_reliability_text({"enough_data": False, "sample_size": 4, "completed": 0})
    good = format_reliability_text({"enough_data": False, "sample_size": 4, "completed": 4})
    assert "0/4" in bad
    assert "4/4" in good


def test_format_full_line_includes_rate_payers_and_latency():
    txt = format_reliability_text(
        {
            "enough_data": True,
            "completion_rate": 0.9,
            "sample_size": 10,
            "distinct_payers": 4,
            "p50_ms": 120,
            "p95_ms": 800,
        }
    )
    assert "90.0%" in txt
    assert "10 runs" in txt
    assert "4 payers" in txt
    assert "120ms p50" in txt
    assert "800ms p95" in txt


def test_format_omits_latency_when_absent():
    txt = format_reliability_text(
        {
            "enough_data": True,
            "completion_rate": 1.0,
            "sample_size": 7,
            "distinct_payers": 5,
            "p50_ms": None,
            "p95_ms": None,
        }
    )
    assert "100.0%" in txt
    assert "latency" not in txt


def test_format_has_no_em_dash():
    txt = format_reliability_text(
        {
            "enough_data": True,
            "completion_rate": 0.5,
            "sample_size": 8,
            "distinct_payers": 2,
            "p50_ms": 10,
            "p95_ms": 20,
        }
    )
    assert "—" not in txt
