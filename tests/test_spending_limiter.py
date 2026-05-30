"""Tests for the spending limiter.

These tests mock the database layer so they run without PostgreSQL.
They verify the limit-checking logic, not the DB queries.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from conduit.services.spending_limiter import (
    check_spending_limits,
    SpendingLimitExceeded,
    ConfirmationRequired,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _mock_settings(**overrides):
    """Create a mock settings object with spending limits."""
    defaults = {
        "spending_limit_per_payment_sats": 10_000,
        "spending_limit_hourly_sats": 50_000,
        "spending_limit_daily_sats": 200_000,
        "spending_confirm_above_sats": 5_000,
        "conduit_api_key": "test-api-key-for-hmac",
    }
    defaults.update(overrides)
    mock = MagicMock()
    for k, v in defaults.items():
        setattr(mock, k, v)
    return mock


# ── Per-payment limit ─────────────────────────────────────────────────


class TestPerPaymentLimit:
    """Tests for the single-payment cap."""

    @pytest.mark.asyncio
    async def test_blocks_over_per_payment_limit(self):
        """A payment exceeding per-payment limit should be blocked."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0), \
             patch("conduit.services.spending_limiter._log_blocked", new_callable=AsyncMock):
            with pytest.raises(SpendingLimitExceeded) as exc_info:
                await check_spending_limits(
                    amount_sats=15_000,
                    tool_name="pay_invoice",
                )
            assert exc_info.value.limit_sats == 10_000
            assert exc_info.value.requested_sats == 15_000

    @pytest.mark.asyncio
    async def test_allows_under_per_payment_limit(self):
        """A payment under the per-payment limit should pass."""
        with patch("conduit.services.spending_limiter.settings",
                    _mock_settings(spending_confirm_above_sats=0)), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0), \
             patch("conduit.services.spending_limiter._reserve", new_callable=AsyncMock, return_value="res-1"):
            await check_spending_limits(
                amount_sats=5_000,
                tool_name="pay_invoice",
                payment_hash="hash-test",
            )

    @pytest.mark.asyncio
    async def test_allows_exact_per_payment_limit(self):
        """A payment exactly at the per-payment limit should pass (no confirm prompt)."""
        with patch("conduit.services.spending_limiter.settings",
                    _mock_settings(spending_confirm_above_sats=0)), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0), \
             patch("conduit.services.spending_limiter._reserve", new_callable=AsyncMock, return_value="res-1"):
            await check_spending_limits(
                amount_sats=10_000,
                tool_name="pay_invoice",
                payment_hash="hash-test",
            )

    @pytest.mark.asyncio
    async def test_zero_limit_disables_check(self):
        """Per-payment limit of 0 should disable the check."""
        with patch("conduit.services.spending_limiter.settings",
                    _mock_settings(spending_limit_per_payment_sats=0,
                                   spending_limit_hourly_sats=0,
                                   spending_limit_daily_sats=0,
                                   spending_confirm_above_sats=0)), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0), \
             patch("conduit.services.spending_limiter._reserve", new_callable=AsyncMock, return_value="res-1"):
            await check_spending_limits(
                amount_sats=999_999,
                tool_name="pay_invoice",
            )


# ── Hourly rolling window ────────────────────────────────────────────


class TestHourlyLimit:
    """Tests for the hourly spending cap."""

    @pytest.mark.asyncio
    async def test_blocks_when_hourly_exceeded(self):
        """Payment should be blocked if it would push hourly total over limit."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=45_000), \
             patch("conduit.services.spending_limiter._log_blocked", new_callable=AsyncMock):
            with pytest.raises(SpendingLimitExceeded) as exc_info:
                await check_spending_limits(
                    amount_sats=6_000,
                    tool_name="pay_invoice",
                    payment_hash="hash-test",
                )
            assert "hourly" in exc_info.value.reason.lower()
            assert exc_info.value.current_sats == 45_000

    @pytest.mark.asyncio
    async def test_allows_when_hourly_has_room(self):
        """Payment should pass if hourly total stays under limit."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=40_000), \
             patch("conduit.services.spending_limiter._reserve", new_callable=AsyncMock, return_value="res-1"):
            await check_spending_limits(
                amount_sats=5_000,
                tool_name="pay_invoice",
                payment_hash="hash-test",
            )


# ── Daily rolling window ─────────────────────────────────────────────


class TestDailyLimit:
    """Tests for the daily spending cap."""

    @pytest.mark.asyncio
    async def test_blocks_when_daily_exceeded(self):
        """Payment should be blocked if it would push daily total over limit."""
        spent_values = [0, 195_000]  # hourly=0, daily=195k

        async def mock_spent(window):
            return spent_values.pop(0)

        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", side_effect=mock_spent), \
             patch("conduit.services.spending_limiter._log_blocked", new_callable=AsyncMock):
            with pytest.raises(SpendingLimitExceeded) as exc_info:
                await check_spending_limits(
                    amount_sats=6_000,
                    tool_name="pay_invoice",
                    payment_hash="hash-test",
                )
            assert "daily" in exc_info.value.reason.lower()


# ── Confirmation threshold ────────────────────────────────────────────


class TestConfirmationThreshold:
    """Tests for the confirmation prompt on large payments."""

    @pytest.mark.asyncio
    async def test_requires_confirmation_above_threshold(self):
        """Payments above the confirm threshold should raise ConfirmationRequired."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0):
            with pytest.raises(ConfirmationRequired) as exc_info:
                await check_spending_limits(
                    amount_sats=7_000,
                    tool_name="pay_invoice",
                    payment_hash="hash-A",
                )
            assert exc_info.value.amount_sats == 7_000
            assert exc_info.value.threshold_sats == 5_000
            # Server must mint a token, not echo a caller-supplied one
            assert exc_info.value.confirmation_token
            assert len(exc_info.value.confirmation_token) >= 16

    @pytest.mark.asyncio
    async def test_token_round_trip_unlocks_payment(self):
        """First call mints a token; second call presenting it passes."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0), \
             patch("conduit.services.spending_limiter._reserve", new_callable=AsyncMock, return_value="res-1"):
            with pytest.raises(ConfirmationRequired) as exc_info:
                await check_spending_limits(
                    amount_sats=7_000,
                    tool_name="pay_invoice",
                    payment_hash="hash-A",
                )
            token = exc_info.value.confirmation_token
            # Second call with the right token passes (no raise)
            await check_spending_limits(
                amount_sats=7_000,
                tool_name="pay_invoice",
                payment_hash="hash-A",
                confirmation_token=token,
            )

    @pytest.mark.asyncio
    async def test_token_is_bound_to_amount(self):
        """A token for one payment can't be used to authorize a different amount."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0):
            with pytest.raises(ConfirmationRequired) as e1:
                await check_spending_limits(
                    amount_sats=7_000, tool_name="pay_invoice", payment_hash="hash-A",
                )
            token = e1.value.confirmation_token
            # Same token, different amount — must NOT pass.
            with pytest.raises(ConfirmationRequired):
                await check_spending_limits(
                    amount_sats=9_000, tool_name="pay_invoice", payment_hash="hash-A",
                    confirmation_token=token,
                )

    @pytest.mark.asyncio
    async def test_token_is_bound_to_payment_hash(self):
        """A token for one invoice can't authorize a different invoice."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0):
            with pytest.raises(ConfirmationRequired) as e1:
                await check_spending_limits(
                    amount_sats=7_000, tool_name="pay_invoice", payment_hash="hash-A",
                )
            token = e1.value.confirmation_token
            with pytest.raises(ConfirmationRequired):
                await check_spending_limits(
                    amount_sats=7_000, tool_name="pay_invoice", payment_hash="hash-B",
                    confirmation_token=token,
                )

    @pytest.mark.asyncio
    async def test_garbage_token_does_not_authorize(self):
        """Caller-fabricated tokens are rejected — regression for the
        original `confirmed=True` self-attestation bypass."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0):
            with pytest.raises(ConfirmationRequired):
                await check_spending_limits(
                    amount_sats=7_000, tool_name="pay_invoice", payment_hash="hash-A",
                    confirmation_token="i-made-this-up",
                )

    @pytest.mark.asyncio
    async def test_no_confirmation_under_threshold(self):
        """Payments under the confirm threshold should not require confirmation."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock, return_value=0), \
             patch("conduit.services.spending_limiter._reserve", new_callable=AsyncMock, return_value="res-1"):
            await check_spending_limits(
                amount_sats=4_000,
                tool_name="pay_invoice",
            )


# ── Edge cases ────────────────────────────────────────────────────────


class TestSpendingEdgeCases:
    """Edge cases and type safety."""

    @pytest.mark.asyncio
    async def test_per_payment_checked_before_hourly(self):
        """Per-payment limit should be checked first (before any DB query)."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._log_blocked", new_callable=AsyncMock):
            with patch("conduit.services.spending_limiter._get_spent_in_window", new_callable=AsyncMock) as mock_spent:
                with pytest.raises(SpendingLimitExceeded):
                    await check_spending_limits(
                        amount_sats=99_999,
                        tool_name="pay_invoice",
                    )
                mock_spent.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_message_includes_amounts(self):
        """Error messages should include human-readable amounts."""
        with patch("conduit.services.spending_limiter.settings", _mock_settings()), \
             patch("conduit.services.spending_limiter._log_blocked", new_callable=AsyncMock):
            with pytest.raises(SpendingLimitExceeded) as exc_info:
                await check_spending_limits(
                    amount_sats=15_000,
                    tool_name="pay_invoice",
                )
            assert "15,000" in exc_info.value.reason
            assert "10,000" in exc_info.value.reason
