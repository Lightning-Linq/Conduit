"""Tests for the sliding window rate limiter — in-memory, Redis, and fallback."""

import pytest
from datetime import timedelta
from time import monotonic
from collections import deque
from unittest.mock import patch, MagicMock

from conduit.services.rate_limiter import (
    SlidingWindowRateLimiter,
    InMemoryBackend,
    RateLimitExceeded,
    TOOL_RATE_LIMITS,
    DEFAULT_RATE_LIMIT,
    _REDIS_PREFIX,
)
from conduit.api.middleware.rate_limit import _resolve_tool, _extract_client_id


# =============================================================================
# In-memory backend tests (existing behavior preserved)
# =============================================================================


class TestInMemoryBackend:
    """Unit tests for the in-memory sliding window backend."""

    def setup_method(self):
        self.backend = InMemoryBackend()

    def test_allows_first_call(self):
        """First call should always succeed."""
        count, _ = self.backend.check_and_record("test", 10, 60)
        assert count == 1

    def test_allows_up_to_limit(self):
        for _ in range(5):
            self.backend.check_and_record("test", 5, 60)

    def test_blocks_after_limit(self):
        for _ in range(5):
            self.backend.check_and_record("test", 5, 60)
        with pytest.raises(RateLimitExceeded):
            self.backend.check_and_record("test", 5, 60)

    def test_expired_entries_pruned(self):
        """Entries older than the window should not count."""
        # Inject expired timestamps directly
        expired = monotonic() - 120
        self.backend._windows["test"] = deque([expired] * 5)
        # Should succeed because all entries are expired
        self.backend.check_and_record("test", 5, 60)

    def test_get_count(self):
        self.backend.check_and_record("test", 10, 60)
        self.backend.check_and_record("test", 10, 60)
        assert self.backend.get_count("test", 60) == 2

    def test_get_tracked_keys(self):
        self.backend.check_and_record("tool_a", 10, 60)
        self.backend.check_and_record("tool_b", 10, 60)
        keys = self.backend.get_tracked_keys()
        assert "tool_a" in keys
        assert "tool_b" in keys


# =============================================================================
# SlidingWindowRateLimiter integration (uses in-memory when no Redis)
# =============================================================================


class TestSlidingWindowRateLimiter:
    """Tests for the unified limiter — falls back to memory without Redis."""

    def setup_method(self):
        """Create a limiter with Redis disabled."""
        # Use a bogus URL so Redis connection fails → in-memory fallback
        self.limiter = SlidingWindowRateLimiter(redis_url="redis://localhost:1/0")

    def test_backend_is_memory_when_redis_unavailable(self):
        assert self.limiter.backend_type == "memory"

    def test_allows_first_call(self):
        self.limiter.check("get_balance")

    def test_allows_up_to_limit(self):
        max_calls, _ = TOOL_RATE_LIMITS["register_skill"]
        for _ in range(max_calls):
            self.limiter.check("register_skill")

    def test_blocks_after_limit(self):
        max_calls, _ = TOOL_RATE_LIMITS["register_skill"]
        for _ in range(max_calls):
            self.limiter.check("register_skill")
        with pytest.raises(RateLimitExceeded) as exc_info:
            self.limiter.check("register_skill")
        assert "register_skill" in str(exc_info.value)
        assert "Try again in" in str(exc_info.value)

    def test_default_limit_for_unknown_tool(self):
        for _ in range(DEFAULT_RATE_LIMIT):
            self.limiter.check("some_future_tool")
        with pytest.raises(RateLimitExceeded):
            self.limiter.check("some_future_tool")

    def test_expired_calls_are_pruned(self):
        max_calls, window = TOOL_RATE_LIMITS["register_skill"]
        key = "global:register_skill"
        expired_time = monotonic() - window.total_seconds() - 1
        self.limiter._memory._windows[key] = deque([expired_time] * max_calls)
        self.limiter.check("register_skill")

    def test_mixed_expired_and_current(self):
        max_calls, window = TOOL_RATE_LIMITS["register_skill"]
        key = "global:register_skill"
        now = monotonic()
        expired_time = now - window.total_seconds() - 1
        current_count = max_calls - 1
        timestamps = [expired_time] * 10 + [now] * current_count
        self.limiter._memory._windows[key] = deque(timestamps)
        self.limiter.check("register_skill")
        with pytest.raises(RateLimitExceeded):
            self.limiter.check("register_skill")

    def test_tools_have_independent_windows(self):
        max_calls, _ = TOOL_RATE_LIMITS["register_skill"]
        for _ in range(max_calls):
            self.limiter.check("register_skill")
        with pytest.raises(RateLimitExceeded):
            self.limiter.check("register_skill")
        # Different tool should still work
        self.limiter.check("discover_skills")

    def test_get_status_fresh_tool(self):
        status = self.limiter.get_status("get_balance")
        assert status["calls_in_window"] == 0
        assert status["remaining"] == 60
        assert status["backend"] == "memory"

    def test_get_status_after_calls(self):
        self.limiter.check("pay_invoice")
        self.limiter.check("pay_invoice")
        self.limiter.check("pay_invoice")
        status = self.limiter.get_status("pay_invoice")
        assert status["calls_in_window"] == 3
        max_calls, _ = TOOL_RATE_LIMITS["pay_invoice"]
        assert status["remaining"] == max_calls - 3

    def test_get_all_status(self):
        self.limiter.check("get_balance")
        self.limiter.check("pay_invoice")
        statuses = self.limiter.get_all_status()
        tool_names = [s["tool"] for s in statuses]
        assert "get_balance" in tool_names
        assert "pay_invoice" in tool_names


# =============================================================================
# Per-client isolation
# =============================================================================


class TestPerClientRateLimiting:
    """Each client (API key) should get its own independent window."""

    def setup_method(self):
        self.limiter = SlidingWindowRateLimiter(redis_url="redis://localhost:1/0")

    def test_different_clients_have_separate_limits(self):
        """Two clients should each get their full allowance."""
        max_calls, _ = TOOL_RATE_LIMITS["register_skill"]

        # Client A uses up all calls
        for _ in range(max_calls):
            self.limiter.check("register_skill", client_id="client_a")

        with pytest.raises(RateLimitExceeded):
            self.limiter.check("register_skill", client_id="client_a")

        # Client B should still be able to call
        self.limiter.check("register_skill", client_id="client_b")

    def test_client_id_in_status(self):
        """Status should report the client_id."""
        self.limiter.check("get_balance", client_id="abc123")
        status = self.limiter.get_status("get_balance", client_id="abc123")
        assert status["client_id"] == "abc123"
        assert status["calls_in_window"] == 1

    def test_global_and_client_are_separate(self):
        """Global (no client_id) and per-client counters are independent."""
        max_calls, _ = TOOL_RATE_LIMITS["register_skill"]

        for _ in range(max_calls):
            self.limiter.check("register_skill")  # global

        # Global is maxed
        with pytest.raises(RateLimitExceeded):
            self.limiter.check("register_skill")

        # Per-client should still work
        self.limiter.check("register_skill", client_id="client_x")


# =============================================================================
# Redis fallback behavior
# =============================================================================


class TestRedisFallback:
    """When Redis fails mid-flight, limiter should degrade to in-memory."""

    def test_redis_error_falls_back_to_memory(self):
        """A Redis error during check should not crash — use memory instead."""
        limiter = SlidingWindowRateLimiter(redis_url="redis://localhost:1/0")
        # Simulate: pretend Redis was available but then fails
        mock_redis_backend = MagicMock()
        mock_redis_backend.check_and_record.side_effect = Exception("Connection lost")
        limiter._redis_backend = mock_redis_backend
        limiter._redis_available = True

        # Should not raise — falls back to memory
        import redis as redis_lib
        mock_redis_backend.check_and_record.side_effect = redis_lib.RedisError("gone")
        limiter.check("get_balance")
        assert limiter._redis_available is False  # marked as down

    def test_reconnect_redis(self):
        """reconnect_redis should attempt to re-establish the connection."""
        limiter = SlidingWindowRateLimiter(redis_url="redis://localhost:1/0")
        assert limiter.backend_type == "memory"
        # Reconnect will fail (no Redis), but should not crash
        result = limiter.reconnect_redis()
        assert result is False


# =============================================================================
# Config sanity
# =============================================================================


class TestRateLimitConfig:
    """Verify the rate limit configuration is sensible."""

    def test_write_ops_have_tighter_limits_than_reads(self):
        write_limit, _ = TOOL_RATE_LIMITS["register_skill"]
        read_limit, _ = TOOL_RATE_LIMITS["discover_skills"]
        assert write_limit < read_limit

    def test_admin_ops_have_tight_limits(self):
        admin_limit, _ = TOOL_RATE_LIMITS["create_macaroon"]
        assert admin_limit <= 5

    def test_all_mcp_tools_have_rate_limits(self):
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
            "create_l402_token", "verify_l402_token", "get_l402_status",
        ]
        for tool in expected_tools:
            assert tool in TOOL_RATE_LIMITS, f"Missing rate limit config for {tool}"


# =============================================================================
# Middleware routing tests
# =============================================================================


class TestRateLimitMiddlewareRouting:
    """Tests for the middleware route → tool name resolution."""

    # Lightning
    def test_get_node_info(self):
        assert _resolve_tool("GET", "/api/v1/lightning/node-info") == "get_node_info"

    def test_get_balance(self):
        assert _resolve_tool("GET", "/api/v1/lightning/balance") == "get_balance"

    def test_create_invoice(self):
        assert _resolve_tool("POST", "/api/v1/lightning/invoices") == "create_invoice"

    def test_decode_invoice(self):
        assert _resolve_tool("POST", "/api/v1/lightning/invoices/decode") == "decode_invoice"

    def test_pay_invoice(self):
        assert _resolve_tool("POST", "/api/v1/lightning/payments") == "pay_invoice"

    def test_check_payment(self):
        assert _resolve_tool("GET", "/api/v1/lightning/payments/abc123") == "check_payment"

    # Marketplace
    def test_discover_skills(self):
        assert _resolve_tool("GET", "/api/v1/marketplace/skills") == "discover_skills"

    def test_register_skill(self):
        assert _resolve_tool("POST", "/api/v1/marketplace/skills") == "register_skill"

    def test_get_skill_details(self):
        assert _resolve_tool("GET", "/api/v1/marketplace/skills/some-uuid") == "get_skill_details"

    def test_request_execution(self):
        assert _resolve_tool("POST", "/api/v1/marketplace/executions") == "request_skill_execution"

    def test_confirm_execution(self):
        assert _resolve_tool("POST", "/api/v1/marketplace/executions/some-uuid/confirm") == "confirm_skill_execution"

    def test_submit_rating(self):
        assert _resolve_tool("POST", "/api/v1/marketplace/executions/some-uuid/rate") == "submit_rating"

    # Security
    def test_spending(self):
        assert _resolve_tool("GET", "/api/v1/security/spending") == "get_spending_status"

    def test_create_macaroon(self):
        assert _resolve_tool("POST", "/api/v1/security/macaroons") == "create_macaroon"

    def test_permissions(self):
        assert _resolve_tool("GET", "/api/v1/security/permissions") == "list_permissions"

    def test_anomalies(self):
        assert _resolve_tool("GET", "/api/v1/security/anomalies") == "get_anomaly_report"

    def test_verification_request(self):
        assert _resolve_tool("POST", "/api/v1/security/verification/request") == "request_verification"

    def test_verification_submit(self):
        assert _resolve_tool("POST", "/api/v1/security/verification/submit") == "submit_verification"

    def test_verification_status(self):
        assert _resolve_tool("GET", "/api/v1/security/verification/some-uuid") == "get_verification_status"

    # Nostr
    def test_nostr_publish(self):
        assert _resolve_tool("POST", "/api/v1/nostr/publish") == "nostr_publish_skill"

    def test_nostr_discover(self):
        assert _resolve_tool("GET", "/api/v1/nostr/discover") == "nostr_discover_skills"

    def test_nostr_profile(self):
        assert _resolve_tool("GET", "/api/v1/nostr/profile") == "nostr_get_profile"

    def test_nostr_relay_status(self):
        assert _resolve_tool("GET", "/api/v1/nostr/relays/status") == "nostr_relay_status"

    # Free routes should not match
    def test_health_not_matched(self):
        assert _resolve_tool("GET", "/health") is None

    def test_root_not_matched(self):
        assert _resolve_tool("GET", "/") is None

    def test_docs_not_matched(self):
        assert _resolve_tool("GET", "/docs") is None

    # Wrong method should not match
    def test_wrong_method(self):
        assert _resolve_tool("DELETE", "/api/v1/lightning/balance") is None


# =============================================================================
# Client ID extraction
# =============================================================================


class TestClientIdExtraction:
    """Tests for extracting client identifiers from requests."""

    def test_extract_from_api_key(self):
        """Should hash the API key to a 16-char hex string."""
        request = MagicMock()
        request.headers = {"x-api-key": "my-secret-key"}
        client_id = _extract_client_id(request)
        assert client_id is not None
        assert len(client_id) == 16
        # Should be deterministic
        assert client_id == _extract_client_id(request)

    def test_different_keys_different_ids(self):
        """Different API keys should produce different client IDs."""
        req1 = MagicMock()
        req1.headers = {"x-api-key": "key-one"}
        req2 = MagicMock()
        req2.headers = {"x-api-key": "key-two"}
        assert _extract_client_id(req1) != _extract_client_id(req2)

    def test_no_api_key_returns_none(self):
        """No API key header should return None (global limiting)."""
        request = MagicMock()
        request.headers = {}
        assert _extract_client_id(request) is None

    def test_api_key_not_stored_raw(self):
        """The raw API key should never appear in the client ID."""
        request = MagicMock()
        key = "super-secret-api-key-12345"
        request.headers = {"x-api-key": key}
        client_id = _extract_client_id(request)
        assert key not in client_id
