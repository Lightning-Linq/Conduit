"""Unit tests for the REQ-02 Phase A rating-prompt policy.

Pure-policy and builder tests only; no Postgres required (the one DB lookup is
mocked).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from conduit.core.config import settings
from conduit.services.rating_prompt import (
    build_rating_prompt,
    format_rating_prompt_text,
    should_prompt_rating,
)

# =============================================================================
# should_prompt_rating (pure)
# =============================================================================


@pytest.mark.parametrize("amount,is_first", [(1, True), (1, False), (10_000, True), (0, False)])
def test_always_prompts(amount, is_first):
    assert should_prompt_rating(amount, is_first, policy="always") is True


@pytest.mark.parametrize("amount,is_first", [(1, True), (1, False), (10_000, True), (0, False)])
def test_never_prompts(amount, is_first):
    assert should_prompt_rating(amount, is_first, policy="never") is False


def test_first_time_provider_tracks_flag():
    assert should_prompt_rating(1, True, policy="first_time_provider") is True
    assert should_prompt_rating(999_999, False, policy="first_time_provider") is False


def test_above_threshold_is_inclusive():
    assert should_prompt_rating(1000, False, policy="above_threshold", threshold_sats=1000) is True
    assert should_prompt_rating(1001, False, policy="above_threshold", threshold_sats=1000) is True
    assert should_prompt_rating(999, False, policy="above_threshold", threshold_sats=1000) is False


def test_unknown_policy_does_not_prompt():
    # A typo'd policy must never spam users.
    assert should_prompt_rating(10_000, True, policy="bogus") is False


def test_defaults_come_from_settings(monkeypatch):
    monkeypatch.setattr(settings, "rating_prompt_policy", "always")
    assert should_prompt_rating(1, False) is True

    monkeypatch.setattr(settings, "rating_prompt_policy", "never")
    assert should_prompt_rating(1, True) is False

    monkeypatch.setattr(settings, "rating_prompt_policy", "above_threshold")
    monkeypatch.setattr(settings, "rating_prompt_sat_threshold", 500)
    assert should_prompt_rating(500, False) is True
    assert should_prompt_rating(499, False) is False


# =============================================================================
# build_rating_prompt
# =============================================================================


def _exec(amount=100, eid="11111111-1111-1111-1111-111111111111", pubkey=None, name="anonymous"):
    return SimpleNamespace(id=eid, amount_sats=amount, payer_pubkey=pubkey, consumer_name=name)


async def test_build_prompt_skips_db_for_non_first_time_policies(monkeypatch):
    monkeypatch.setattr(settings, "rating_prompt_policy", "always")
    session = AsyncMock()
    payload = await build_rating_prompt(session, SimpleNamespace(provider_name="p"), _exec())

    assert payload["should_prompt_rating"] is True
    assert payload["rating_policy"] == "always"
    assert payload["execution_id"] == "11111111-1111-1111-1111-111111111111"
    session.execute.assert_not_called()  # no DB hit unless policy needs it


async def test_build_prompt_first_time_customer_is_prompted(monkeypatch):
    monkeypatch.setattr(settings, "rating_prompt_policy", "first_time_provider")
    result = MagicMock()
    result.scalar.return_value = 0  # no prior completed executions for this provider
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    payload = await build_rating_prompt(session, SimpleNamespace(provider_name="p"), _exec())

    assert payload["should_prompt_rating"] is True
    session.execute.assert_awaited_once()


async def test_build_prompt_repeat_customer_not_prompted(monkeypatch):
    monkeypatch.setattr(settings, "rating_prompt_policy", "first_time_provider")
    result = MagicMock()
    result.scalar.return_value = 3  # prior completed executions exist
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    payload = await build_rating_prompt(session, SimpleNamespace(provider_name="p"), _exec())

    assert payload["should_prompt_rating"] is False


# =============================================================================
# format_rating_prompt_text
# =============================================================================


def test_format_always_surfaces_execution_id_without_nudge():
    txt = format_rating_prompt_text({"execution_id": "abc", "should_prompt_rating": False})
    assert "Execution ID: abc" in txt
    assert "submit_rating" not in txt


def test_format_adds_nudge_when_prompting():
    txt = format_rating_prompt_text({"execution_id": "abc", "should_prompt_rating": True})
    assert "Execution ID: abc" in txt
    assert "submit_rating" in txt
    assert "1-5" in txt


def test_format_has_no_em_dash():
    txt = format_rating_prompt_text({"execution_id": "abc", "should_prompt_rating": True})
    assert "—" not in txt
