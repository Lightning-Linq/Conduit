"""Tests for the platform fee calculator.

Fee-inclusive model: the buyer pays exactly the listed price; the platform
fee is carved OUT of that price, so the provider nets price - fee. Prices
below the waive floor carry no fee at all.
"""

from unittest.mock import patch

import pytest

from conduit.services.fee_calculator import calculate_fee


class TestFeeCalculation:
    """Test fee calculation logic (seller-pays, fee-inclusive)."""

    def test_basic_fee_calculation(self):
        """1.5% of 100 sats = 2 sats (ceiling), carved out of the price."""
        fee = calculate_fee(100)
        assert fee.skill_price_sats == 100
        assert fee.platform_fee_sats == 2  # ceil(1.5) = 2
        assert fee.provider_amount_sats == 98
        assert fee.total_consumer_cost_sats == 100  # buyer pays the listed price
        assert fee.fee_percent == 1.5
        assert fee.fee_enabled is True

    def test_large_amount(self):
        """1.5% of 10000 sats = 150 sats."""
        fee = calculate_fee(10000)
        assert fee.platform_fee_sats == 150
        assert fee.provider_amount_sats == 9850
        assert fee.total_consumer_cost_sats == 10000

    def test_waive_floor_boundary_fee_applies(self):
        """At the waive floor (10 sats) the fee applies: minimum 1 sat."""
        fee = calculate_fee(10)
        assert fee.platform_fee_sats == 1
        assert fee.provider_amount_sats == 9
        assert fee.total_consumer_cost_sats == 10

    def test_below_waive_floor_no_fee(self):
        """Prices below the waive floor carry no fee — provider gets it all."""
        for price in (1, 5, 9):
            fee = calculate_fee(price)
            assert fee.platform_fee_sats == 0
            assert fee.provider_amount_sats == price
            assert fee.total_consumer_cost_sats == price
            assert fee.fee_enabled is False

    def test_free_skill_no_fee(self):
        """Free skills (0 sats) should not incur a fee."""
        fee = calculate_fee(0)
        assert fee.platform_fee_sats == 0
        assert fee.provider_amount_sats == 0
        assert fee.total_consumer_cost_sats == 0
        assert fee.fee_enabled is False

    @patch("conduit.services.fee_calculator.settings")
    def test_fee_disabled(self, mock_settings):
        """When platform_fee_enabled is False, fee should be 0."""
        mock_settings.platform_fee_enabled = False
        mock_settings.transaction_fee_percent = 1.5
        mock_settings.platform_fee_minimum_sats = 1
        mock_settings.platform_fee_waive_below_sats = 10

        fee = calculate_fee(1000)
        assert fee.platform_fee_sats == 0
        assert fee.provider_amount_sats == 1000
        assert fee.total_consumer_cost_sats == 1000
        assert fee.fee_enabled is False

    @patch("conduit.services.fee_calculator.settings")
    def test_custom_fee_percent(self, mock_settings):
        """Test with a different fee percentage."""
        mock_settings.platform_fee_enabled = True
        mock_settings.transaction_fee_percent = 3.0
        mock_settings.platform_fee_minimum_sats = 1
        mock_settings.platform_fee_waive_below_sats = 10

        fee = calculate_fee(200)
        assert fee.platform_fee_sats == 6  # 3% of 200 = 6
        assert fee.provider_amount_sats == 194
        assert fee.total_consumer_cost_sats == 200
        assert fee.fee_percent == 3.0

    @patch("conduit.services.fee_calculator.settings")
    def test_custom_minimum(self, mock_settings):
        """Minimum fee overrides percentage when calculated fee is lower."""
        mock_settings.platform_fee_enabled = True
        mock_settings.transaction_fee_percent = 0.1  # very low
        mock_settings.platform_fee_minimum_sats = 5
        mock_settings.platform_fee_waive_below_sats = 10

        fee = calculate_fee(100)
        # 0.1% of 100 = 0.1 -> ceil = 1, but minimum is 5
        assert fee.platform_fee_sats == 5
        assert fee.provider_amount_sats == 95
        assert fee.total_consumer_cost_sats == 100

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

    def test_provider_never_nets_zero_at_or_above_floor(self):
        """Invariant: for any price at/above the waive floor, provider nets >= 1 sat."""
        for price in (10, 11, 12, 25, 67, 100, 1000, 5000):
            fee = calculate_fee(price)
            assert fee.provider_amount_sats >= 1, f"provider zeroed at {price}"
            assert fee.provider_amount_sats + fee.platform_fee_sats == price

    def test_split_always_sums_to_price(self):
        """provider_amount + fee == price for every path (no sats created/lost)."""
        for price in (0, 1, 9, 10, 100, 333, 10000):
            fee = calculate_fee(price)
            assert fee.provider_amount_sats + fee.platform_fee_sats == max(price, 0)
            assert fee.total_consumer_cost_sats == max(price, 0)

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
