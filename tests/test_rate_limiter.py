"""Tests for the sliding window rate limiter."""

import pytest
from datetime import timedelta
from time import monotonic
from collections import deque

from conduit.services.rate_limiter import (
    SlidingWindowRateLimiter,
    RateLimitExceeded,
    TOOL_RATE_LIMITS,
    DEFAULT_RATE_LIMIT,
)


class TestSlidingWindowRateLimiter:
    """Unit tests for the rate limiter — no I/O, pure in-memory logic."""

    def setup_method(self):
        """Fresh limiter for each test."""
        self.limiter = SlidingWindowRateLimiter()

    # ── Basic allow / deny ────────────────────────────────────────────

    def test_allows_first_call(self):
        """First call to any tool should always be allowed."""
        self.limiter.check("get_balance")  # should not raise

    def test_allows_up_to_limit(self):
        """Calls up to the configured limit should all pass."""
        max_calls, _ = TOOL_RATE_LIMITS["register_skill"]  # 5 per 10min
        for _ in range(max_calls):
            self.limiter.check("register_skill")

    def test_blocks_after_limit(self):
        """One call past the limit should raise RateLimitExceeded."""
        max_calls, _ = TOOL_RATE_LIMITS["register_skill"]
        for _ in range(max_calls):
            self.limiter.check("register_skill")

        with pytest.raises(RateLimitExceeded) as exc_info:
            self.limiter.check("register_skill")
        assert "register_skill" in str(exc_info.value)
        assert "Try again in" in str(exc_info.value)

    def test_default_limit_for_unknown_tool(self):
        """Tools without explicit config get the default limit."""
        for _ in range(DEFAULT_RATE_LIMIT):
            self.limiter.check("some_future_tool")

        with pytest.raises(RateLimitExceeded):
            self.limiter.check("some_future_tool")

    # ── Window expiry ─────────────────────────────────────────────────

    def test_expired_calls_are_pruned(self):
        """Calls older than the window should be pruned and not count."""
        max_calls, window = TOOL_RATE_LIMITS["register_skill"]

        # Manually inject timestamps that are already expired
        expired_time = monotonic() - window.total_seconds() - 1
        self.limiter._windows["register_skill"] = deque(
            [expired_time] * max_calls
        )

        # Should be allowed because all entries are expired
        self.limiter.check("register_skill")

    def test_mixed_expired_and_current(self):
        """Only non-expired calls should count toward the limit."""
        max_calls, window = TOOL_RATE_LIMITS["register_skill"]

        now = monotonic()
        expired_time = now - window.total_seconds() - 1

        # Fill with expired + some current
        current_count = max_calls - 1
        timestamps = [expired_time] * 10 + [now] * current_count
        self.limiter._windows["register_skill"] = deque(timestamps)

        # Should allow one more (current_count is max_calls - 1)
        self.limiter.check("register_skill")

        # But not two more
        with pytest.raises(RateLimitExceeded):
            self.limiter.check("register_skill")

    # ── Tool isolation ────────────────────────────────────────────────

    def test_tools_have_independent_windows(self):
        """Hitting the limit on one tool should not affect another."""
        max_calls, _ = TOOL_RATE_LIMITS["register_skill"]
        for _ in range(max_calls):
            self.limiter.check("register_skill")

        # register_skill is maxed out
        with pytest.raises(RateLimitExceeded):
            self.limiter.check("register_skill")

        # But discover_skills should still work fine
        self.limiter.check("discover_skills")

    # ── Status reporting ──────────────────────────────────────────────

    def test_get_status_fresh_tool(self):
        """Status for an uncalled tool should show 0 calls."""
        status = self.limiter.get_status("get_balance")
        assert status["calls_in_window"] == 0
        assert status["remaining"] == 60  # read ops get 60/min

    def test_get_status_after_calls(self):
        """Status should reflect the number of calls made."""
        self.limiter.check("pay_invoice")
        self.limiter.check("pay_invoice")
        self.limiter.check("pay_invoice")

        status = self.limiter.get_status("pay_invoice")
        assert status["calls_in_window"] == 3
        max_calls, _ = TOOL_RATE_LIMITS["pay_invoice"]
        assert status["remaining"] == max_calls - 3

    def test_get_all_status(self):
        """get_all_status should return entries for all called tools."""
        self.limiter.check("get_balance")
        self.limiter.check("pay_invoice")

        statuses = self.limiter.get_all_status()
        tool_names = [s["tool"] for s in statuses]
        assert "get_balance" in tool_names
        assert "pay_invoice" in tool_names

    # ── Config sanity ─────────────────────────────────────────────────

    def test_write_ops_have_tighter_limits_than_reads(self):
        """Write/admin operations should have stricter limits than reads."""
        write_limit, _ = TOOL_RATE_LIMITS["register_skill"]
        read_limit, _ = TOOL_RATE_LIMITS["discover_skills"]
        assert write_limit < read_limit

    def test_admin_ops_have_tight_limits(self):
        """Admin operations like create_macaroon should be tightly limited."""
        admin_limit, _ = TOOL_RATE_LIMITS["create_macaroon"]
        assert admin_limit <= 5

    def test_all_mcp_tools_have_rate_limits(self):
        """Every known MCP tool should have an explicit rate limit configured."""
        expected_tools = [
            "get_node_info", "get_balance", "create_invoice", "pay_invoice",
            "decode_invoice", "check_payment", "discover_skills",
            "get_skill_details", "register_skill", "request_skill_execution",
            "confirm_skill_execution", "submit_rating", "request_verification",
            "submit_verification", "get_verification_status",
            "get_spending_status", "create_macaroon", "get_anomaly_report",
            "list_permissions",
            "nostr_publish_skill", "nostr_discover_skills",
            "nostr_get_profile", "nostr_relay_status",
        ]
        for tool in expected_tools:
            assert tool in TOOL_RATE_LIMITS, f"Missing rate limit config for {tool}"
