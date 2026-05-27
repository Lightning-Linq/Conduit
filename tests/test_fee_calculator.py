"""Tests for the platform fee calculator."""

import math
from unittest.mock import patch

import pytest

from conduit.services.fee_calculator import calculate_fee, FeeBreakdown


class TestFeeCalculation:
    """Test fee calculation logic."""

    def test_basic_fee_calculation(self):
        """1.5% of 100 sats = 2 sats (ceiling)."""
        fee = calculate_fee(100)
        assert fee.skill_price_sats == 100
        assert fee.platform_fee_sats == 2  # ceil(1.5) = 2
        assert fee.total_consumer_cost_sats == 102
        assert fee.fee_percent == 1.5
        assert fee.fee_enabled is True

    def test_large_amount(self):
        """1.5% of 10000 sats = 150 sats."""
        fee = calculate_fee(10000)
        assert fee.platform_fee_sats == 150
        assert fee.total_consumer_cost_sats == 10150

    def test_small_amount_minimum_fee(self):
        """Fee on 10 sats = ceil(0.15) = 1 sat (matches minimum)."""
        fee = calculate_fee(10)
        assert fee.platform_fee_sats == 1
        assert fee.total_consumer_cost_sats == 11

    def test_very_small_amount_floor_at_minimum(self):
        """Fee on 1 sat: ceil(0.015) = 1 sat (minimum kicks in)."""
        fee = calculate_fee(1)
        assert fee.platform_fee_sats == 1
        assert fee.total_consumer_cost_sats == 2

    def test_free_skill_no_fee(self):
        """Free skills (0 sats) should not incur a fee."""
        fee = calculate_fee(0)
        assert fee.platform_fee_sats == 0
        assert fee.total_consumer_cost_sats == 0
        assert fee.fee_enabled is False

    @patch("conduit.services.fee_calculator.settings")
    def test_fee_disabled(self, mock_settings):
        """When platform_fee_enabled is False, fee should be 0."""
        mock_settings.platform_fee_enabled = False
        mock_settings.transaction_fee_percent = 1.5
        mock_settings.platform_fee_minimum_sats = 1

        fee = calculate_fee(1000)
        assert fee.platform_fee_sats == 0
        assert fee.total_consumer_cost_sats == 1000
        assert fee.fee_enabled is False

    @patch("conduit.services.fee_calculator.settings")
    def test_custom_fee_percent(self, mock_settings):
        """Test with a different fee percentage."""
        mock_settings.platform_fee_enabled = True
        mock_settings.transaction_fee_percent = 3.0
        mock_settings.platform_fee_minimum_sats = 1

        fee = calculate_fee(200)
        assert fee.platform_fee_sats == 6  # 3% of 200 = 6
        assert fee.total_consumer_cost_sats == 206
        assert fee.fee_percent == 3.0

    @patch("conduit.services.fee_calculator.settings")
    def test_custom_minimum(self, mock_settings):
        """Minimum fee overrides percentage when calculated fee is lower."""
        mock_settings.platform_fee_enabled = True
        mock_settings.transaction_fee_percent = 0.1  # very low
        mock_settings.platform_fee_minimum_sats = 5

        fee = calculate_fee(100)
        # 0.1% of 100 = 0.1 -> ceil = 1, but minimum is 5
        assert fee.platform_fee_sats == 5
        assert fee.total_consumer_cost_sats == 105

    def test_ceiling_rounding(self):
        """Fee should always round up (ceiling), never down."""
        # 1.5% of 50 = 0.75 -> ceil = 1
        fee = calculate_fee(50)
        assert fee.platform_fee_sats == 1

        # 1.5% of 200 = 3.0 -> exact, no rounding needed
        fee = calculate_fee(200)
        assert fee.platform_fee_sats == 3

        # 1.5% of 333 = 4.995 -> ceil = 5
        fee = calculate_fee(333)
        assert fee.platform_fee_sats == 5

    def test_fee_breakdown_is_frozen(self):
        """FeeBreakdown should be immutable."""
        fee = calculate_fee(100)
        with pytest.raises(AttributeError):
            fee.platform_fee_sats = 999

    def test_negative_price_treated_as_zero(self):
        """Negative prices should result in no fee."""
        fee = calculate_fee(-10)
        assert fee.platform_fee_sats == 0
        assert fee.fee_enabled is False
