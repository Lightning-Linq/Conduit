"""
Sliding-window rate limiter with Redis backend and in-memory fallback.

Each tool (or tool category) gets a max number of calls per window.
Redis sorted sets provide atomic, cross-process, restart-surviving
counters.  If Redis is unavailable, falls back to in-memory deques
so Conduit keeps running (with per-process-only limits).

Cloud-ready: works with local Redis, AWS ElastiCache, Upstash, etc.
— just change REDIS_URL in .env.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from datetime import timedelta

import redis

from conduit.core.config import settings


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
    "admin_stats": (10, timedelta(minutes=1)),
    "admin_reset": (3, timedelta(hours=1)),
    "admin_delete_skill": (10, timedelta(minutes=10)),
    "admin_delete_execution": (10, timedelta(minutes=10)),

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

# Redis key prefix for rate limit sorted sets
_REDIS_PREFIX = "conduit:ratelimit:"


# =============================================================================
# Exceptions
# =============================================================================


class RateLimitExceeded(Exception):
    """Raised when a tool call exceeds its rate limit."""

    def __init__(self, message: str, retry_after_seconds: int = 60):
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


# =============================================================================
# In-memory backend (fallback)
# =============================================================================


class InMemoryBackend:
    """
    In-memory sliding window using deques.

    Used as the primary backend when Redis is not configured, or as
    automatic fallback when Redis becomes unreachable.
    """

    def __init__(self):
        self._windows: dict[str, deque[float]] = {}

    def check_and_record(
        self,
        key: str,
        max_calls: int,
        window_seconds: float,
    ) -> tuple[int, int]:
        """
        Check rate limit and record the call if allowed.

        Returns (current_count, retry_after_seconds).
        Raises RateLimitExceeded if over limit.
        """
        from time import monotonic

        now = monotonic()
        cutoff = now - window_seconds

        if key not in self._windows:
            self._windows[key] = deque()

        dq = self._windows[key]

        # Prune expired entries
        while dq and dq[0] < cutoff:
            dq.popleft()

        if len(dq) >= max_calls:
            oldest = dq[0]
            retry_after = int(oldest + window_seconds - now) + 1
            raise RateLimitExceeded(
                f"Rate limit exceeded: "
                f"{max_calls} calls per {int(window_seconds)}s. "
                f"Try again in {retry_after}s."
            )

        dq.append(now)
        return len(dq), 0

    def get_count(self, key: str, window_seconds: float) -> int:
        """Get current call count within the window."""
        from time import monotonic

        now = monotonic()
        cutoff = now - window_seconds
        dq = self._windows.get(key, deque())
        return sum(1 for ts in dq if ts >= cutoff)

    def get_tracked_keys(self) -> list[str]:
        """Return all keys that have been tracked."""
        return list(self._windows.keys())


# =============================================================================
# Redis backend
# =============================================================================


class RedisBackend:
    """
    Redis-backed sliding window using sorted sets.

    Each rate limit key is a sorted set where:
    - Members are unique call IDs (timestamp-based)
    - Scores are Unix timestamps (time of the call)

    On each check:
    1. ZREMRANGEBYSCORE to prune expired entries
    2. ZCARD to count remaining entries
    3. ZADD to record the new call (if allowed)

    All three operations run in a Redis pipeline for atomicity.
    Works with any Redis-compatible server (local, ElastiCache, Upstash).
    """

    def __init__(self, redis_client: redis.Redis):
        self._redis = redis_client

    def check_and_record(
        self,
        key: str,
        max_calls: int,
        window_seconds: float,
    ) -> tuple[int, int]:
        """
        Atomic check-and-record using a Redis pipeline.

        Returns (current_count, 0) on success.
        Raises RateLimitExceeded if over limit.
        """
        now = time.time()
        cutoff = now - window_seconds
        redis_key = f"{_REDIS_PREFIX}{key}"
        # Unique member: timestamp with enough precision to avoid collisions
        member = f"{now:.6f}"

        pipe = self._redis.pipeline(transaction=True)
        pipe.zremrangebyscore(redis_key, "-inf", cutoff)
        pipe.zcard(redis_key)
        pipe.zadd(redis_key, {member: now})
        pipe.expire(redis_key, int(window_seconds) + 10)  # TTL as safety net
        results = pipe.execute()

        current_count = results[1]  # ZCARD result (before adding new entry)

        if current_count >= max_calls:
            # Over limit — remove the entry we just added
            self._redis.zrem(redis_key, member)

            # Calculate retry_after from the oldest entry
            oldest = self._redis.zrange(redis_key, 0, 0, withscores=True)
            if oldest:
                retry_after = int(oldest[0][1] + window_seconds - now) + 1
            else:
                retry_after = int(window_seconds)

            raise RateLimitExceeded(
                f"Rate limit exceeded: "
                f"{max_calls} calls per {int(window_seconds)}s. "
                f"Try again in {retry_after}s."
            )

        return current_count + 1, 0

    def get_count(self, key: str, window_seconds: float) -> int:
        """Get current call count within the window."""
        now = time.time()
        cutoff = now - window_seconds
        redis_key = f"{_REDIS_PREFIX}{key}"

        pipe = self._redis.pipeline(transaction=True)
        pipe.zremrangebyscore(redis_key, "-inf", cutoff)
        pipe.zcard(redis_key)
        results = pipe.execute()

        return results[1]

    def get_tracked_keys(self) -> list[str]:
        """Return all rate limit keys in Redis."""
        prefix_len = len(_REDIS_PREFIX)
        keys = self._redis.keys(f"{_REDIS_PREFIX}*")
        return [k.decode("utf-8")[prefix_len:] if isinstance(k, bytes) else k[prefix_len:] for k in keys]


# =============================================================================
# Unified rate limiter
# =============================================================================


class SlidingWindowRateLimiter:
    """
    Rate limiter with Redis primary + in-memory fallback.

    On init, tries to connect to Redis. If it succeeds, uses Redis
    for all operations. If Redis is unavailable (not running, wrong URL,
    network error), falls back to in-memory and logs a warning.

    If Redis becomes unavailable mid-flight, automatically degrades to
    in-memory for that call and retries Redis on the next call.
    """

    def __init__(self, redis_url: str | None = None):
        self._memory = InMemoryBackend()
        self._redis_backend: RedisBackend | None = None
        self._redis_url = redis_url or settings.redis_url
        self._redis_available = False

        self._try_connect_redis()

    def _try_connect_redis(self) -> None:
        """Attempt to connect to Redis. Silent on failure."""
        try:
            client = redis.Redis.from_url(
                self._redis_url,
                decode_responses=False,
                socket_connect_timeout=2,
                socket_timeout=2,
                retry_on_timeout=True,
            )
            client.ping()
            self._redis_backend = RedisBackend(client)
            self._redis_available = True
            print(
                f"[rate_limiter] Redis connected ({self._redis_url})",
                file=sys.stderr,
            )
        except Exception as e:
            self._redis_available = False
            self._redis_backend = None
            print(
                f"[rate_limiter] Redis unavailable, using in-memory fallback: {e}",
                file=sys.stderr,
            )

    def _get_key(self, tool_name: str, client_id: str | None = None) -> str:
        """Build the rate limit key, optionally scoped to a client."""
        if client_id:
            return f"{client_id}:{tool_name}"
        return f"global:{tool_name}"

    def check(
        self,
        tool_name: str,
        client_id: str | None = None,
    ) -> None:
        """
        Check if a tool call is allowed. Raises RateLimitExceeded if not.

        Args:
            tool_name: The MCP tool name or REST tool equivalent.
            client_id: Optional client identifier (API key hash, etc.)
                       for per-client limits. If None, uses a global counter.
        """
        max_calls, window = TOOL_RATE_LIMITS.get(
            tool_name, (DEFAULT_RATE_LIMIT, DEFAULT_WINDOW)
        )
        window_seconds = window.total_seconds()
        key = self._get_key(tool_name, client_id)

        # Try Redis first, fall back to memory
        if self._redis_available and self._redis_backend:
            try:
                count, _ = self._redis_backend.check_and_record(
                    key, max_calls, window_seconds,
                )
                print(
                    f"[rate_limiter] {key}: {count}/{max_calls} "
                    f"in {int(window_seconds)}s window (redis)",
                    file=sys.stderr,
                )
                return
            except RateLimitExceeded as e:
                # Re-raise with tool name for better error messages
                raise RateLimitExceeded(
                    f"Rate limit exceeded for '{tool_name}': "
                    f"{max_calls} calls per {int(window_seconds)}s. "
                    f"{str(e).split('. ')[-1]}"
                )
            except redis.RedisError as e:
                # Redis went away mid-flight — degrade gracefully
                print(
                    f"[rate_limiter] Redis error, falling back to memory: {e}",
                    file=sys.stderr,
                )
                self._redis_available = False

        # In-memory fallback
        try:
            count, _ = self._memory.check_and_record(
                key, max_calls, window_seconds,
            )
            print(
                f"[rate_limiter] {key}: {count}/{max_calls} "
                f"in {int(window_seconds)}s window (memory)",
                file=sys.stderr,
            )
        except RateLimitExceeded:
            raise RateLimitExceeded(
                f"Rate limit exceeded for '{tool_name}': "
                f"{max_calls} calls per {int(window_seconds)}s. "
                f"Try again in {int(window_seconds)}s."
            )

    def get_status(
        self,
        tool_name: str,
        client_id: str | None = None,
    ) -> dict:
        """Get current rate limit status for a tool."""
        max_calls, window = TOOL_RATE_LIMITS.get(
            tool_name, (DEFAULT_RATE_LIMIT, DEFAULT_WINDOW)
        )
        window_seconds = window.total_seconds()
        key = self._get_key(tool_name, client_id)

        if self._redis_available and self._redis_backend:
            try:
                current = self._redis_backend.get_count(key, window_seconds)
            except redis.RedisError:
                current = self._memory.get_count(key, window_seconds)
        else:
            current = self._memory.get_count(key, window_seconds)

        return {
            "tool": tool_name,
            "client_id": client_id or "global",
            "calls_in_window": current,
            "max_calls": max_calls,
            "window_seconds": int(window_seconds),
            "remaining": max(0, max_calls - current),
            "backend": "redis" if self._redis_available else "memory",
        }

    def get_all_status(self, client_id: str | None = None) -> list[dict]:
        """Get rate limit status for all tools that have been called."""
        if self._redis_available and self._redis_backend:
            try:
                keys = self._redis_backend.get_tracked_keys()
            except redis.RedisError:
                keys = self._memory.get_tracked_keys()
        else:
            keys = self._memory.get_tracked_keys()

        # Extract tool names from keys (format: "client_id:tool" or "global:tool")
        tool_names = set()
        for k in keys:
            parts = k.split(":", 1)
            if len(parts) == 2:
                tool_names.add(parts[1])
            else:
                tool_names.add(k)

        return [
            self.get_status(tool_name, client_id)
            for tool_name in sorted(tool_names)
        ]

    @property
    def backend_type(self) -> str:
        """Return which backend is currently active."""
        return "redis" if self._redis_available else "memory"

    def reconnect_redis(self) -> bool:
        """
        Attempt to reconnect to Redis. Returns True if successful.

        Useful for health checks or periodic retry after Redis was
        unavailable. Can be called from a background task.
        """
        self._try_connect_redis()
        return self._redis_available


# Singleton instance — created once when the module is imported
rate_limiter = SlidingWindowRateLimiter()
