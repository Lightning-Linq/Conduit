"""
In-memory sliding-window rate limiter for MCP tool calls.

Each tool (or tool category) gets a max number of calls per window.
Timestamps are stored in a deque and expired entries are pruned on
every check, so memory stays bounded.

No external dependencies (no Redis) — suitable for a single-process
MCP server.
"""

import sys
from collections import deque
from datetime import timedelta
from time import monotonic


# =============================================================================
# Configuration — calls per window
# =============================================================================

# Default: 30 calls per minute for most tools
DEFAULT_RATE_LIMIT = 30
DEFAULT_WINDOW = timedelta(minutes=1)

# Per-tool overrides (tool_name -> (max_calls, window))
TOOL_RATE_LIMITS: dict[str, tuple[int, timedelta]] = {
    # Marketplace write operations — tight limits to prevent spam
    "register_skill": (5, timedelta(minutes=10)),

    # Payment operations — moderate limits
    "pay_invoice": (10, timedelta(minutes=1)),
    "create_invoice": (15, timedelta(minutes=1)),

    # Execution operations — moderate limits
    "request_skill_execution": (10, timedelta(minutes=1)),
    "confirm_skill_execution": (10, timedelta(minutes=1)),
    "submit_rating": (10, timedelta(minutes=1)),

    # Verification — moderate limits
    "request_verification": (10, timedelta(minutes=10)),
    "submit_verification": (10, timedelta(minutes=10)),
    "get_verification_status": (60, timedelta(minutes=1)),

    # Security/admin — tight limits
    "create_macaroon": (5, timedelta(minutes=10)),

    # Nostr operations
    "nostr_publish_skill": (5, timedelta(minutes=10)),
    "nostr_discover_skills": (30, timedelta(minutes=1)),
    "nostr_get_profile": (60, timedelta(minutes=1)),
    "nostr_relay_status": (10, timedelta(minutes=1)),

    # L402 operations
    "create_l402_token": (15, timedelta(minutes=1)),
    "verify_l402_token": (60, timedelta(minutes=1)),
    "get_l402_status": (60, timedelta(minutes=1)),

    # Read operations — generous limits
    "discover_skills": (60, timedelta(minutes=1)),
    "get_skill_details": (60, timedelta(minutes=1)),
    "get_node_info": (60, timedelta(minutes=1)),
    "get_balance": (60, timedelta(minutes=1)),
    "decode_invoice": (60, timedelta(minutes=1)),
    "check_payment": (60, timedelta(minutes=1)),
    "get_spending_status": (60, timedelta(minutes=1)),
    "get_anomaly_report": (60, timedelta(minutes=1)),
    "list_permissions": (60, timedelta(minutes=1)),
}


# =============================================================================
# Rate Limiter
# =============================================================================


class RateLimitExceeded(Exception):
    """Raised when a tool call exceeds its rate limit."""
    pass


class SlidingWindowRateLimiter:
    """
    In-memory sliding window rate limiter.

    For each key (tool name), keeps a deque of call timestamps.
    On each check, prunes expired entries, then allows or rejects.
    """

    def __init__(self):
        # key -> deque of monotonic timestamps
        self._windows: dict[str, deque[float]] = {}

    def check(self, tool_name: str) -> None:
        """
        Check if a tool call is allowed. Raises RateLimitExceeded if not.
        """
        max_calls, window = TOOL_RATE_LIMITS.get(
            tool_name, (DEFAULT_RATE_LIMIT, DEFAULT_WINDOW)
        )
        window_seconds = window.total_seconds()
        now = monotonic()
        cutoff = now - window_seconds

        # Get or create the window deque
        if tool_name not in self._windows:
            self._windows[tool_name] = deque()

        dq = self._windows[tool_name]

        # Prune expired entries
        while dq and dq[0] < cutoff:
            dq.popleft()

        # Check limit
        if len(dq) >= max_calls:
            # Calculate when the oldest entry expires
            oldest = dq[0]
            retry_after = int(oldest + window_seconds - now) + 1
            raise RateLimitExceeded(
                f"Rate limit exceeded for '{tool_name}': "
                f"{max_calls} calls per {int(window_seconds)}s. "
                f"Try again in {retry_after}s."
            )

        # Record this call
        dq.append(now)
        print(
            f"[rate_limiter] {tool_name}: {len(dq)}/{max_calls} "
            f"in {int(window_seconds)}s window",
            file=sys.stderr,
        )

    def get_status(self, tool_name: str) -> dict:
        """Get current rate limit status for a tool."""
        max_calls, window = TOOL_RATE_LIMITS.get(
            tool_name, (DEFAULT_RATE_LIMIT, DEFAULT_WINDOW)
        )
        window_seconds = window.total_seconds()
        now = monotonic()
        cutoff = now - window_seconds

        dq = self._windows.get(tool_name, deque())

        # Count non-expired entries
        current = sum(1 for ts in dq if ts >= cutoff)

        return {
            "tool": tool_name,
            "calls_in_window": current,
            "max_calls": max_calls,
            "window_seconds": int(window_seconds),
            "remaining": max(0, max_calls - current),
        }

    def get_all_status(self) -> list[dict]:
        """Get rate limit status for all tools that have been called."""
        return [
            self.get_status(tool_name)
            for tool_name in sorted(self._windows.keys())
        ]


# Singleton instance — created once when the module is imported
rate_limiter = SlidingWindowRateLimiter()
