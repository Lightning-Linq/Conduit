"""
Rate limiting middleware — applies per-client, per-tool rate limits at the
HTTP layer.

Maps each REST route + method to its corresponding MCP tool name, extracts
the client identifier from the X-API-Key header (hashed for privacy), then
delegates to the SlidingWindowRateLimiter. Returns 429 with a Retry-After
header when the limit is exceeded.

This replaces the inline try/except blocks previously duplicated in every
router handler, centralizing rate limiting in one place so new endpoints
get protected automatically.
"""

from __future__ import annotations

import hashlib
import re

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from conduit.services.rate_limiter import rate_limiter, RateLimitExceeded


# =============================================================================
# Route → tool name mapping
# =============================================================================

# Each entry maps (HTTP method, route pattern) to a tool name.
# Patterns are matched top-to-bottom; first match wins.
# Use {param} for path parameters.

_ROUTE_TOOL_MAP: list[tuple[str, str, str]] = [
    # Lightning
    ("GET",  "/api/v1/lightning/node-info",          "get_node_info"),
    ("GET",  "/api/v1/lightning/balance",             "get_balance"),
    ("POST", "/api/v1/lightning/invoices/decode",     "decode_invoice"),
    ("POST", "/api/v1/lightning/invoices",            "create_invoice"),
    ("POST", "/api/v1/lightning/payments",            "pay_invoice"),
    ("GET",  "/api/v1/lightning/payments/{param}",    "check_payment"),

    # Marketplace
    ("GET",  "/api/v1/marketplace/skills",                          "discover_skills"),
    ("POST", "/api/v1/marketplace/skills",                          "register_skill"),
    ("GET",  "/api/v1/marketplace/skills/{param}",                  "get_skill_details"),
    ("POST", "/api/v1/marketplace/executions",                      "request_skill_execution"),
    ("POST", "/api/v1/marketplace/executions/{param}/confirm",      "confirm_skill_execution"),
    ("POST", "/api/v1/marketplace/executions/{param}/rate",         "submit_rating"),

    # Security
    ("GET",  "/api/v1/security/spending",             "get_spending_status"),
    ("POST", "/api/v1/security/macaroons",            "create_macaroon"),
    ("GET",  "/api/v1/security/permissions",           "list_permissions"),
    ("GET",  "/api/v1/security/anomalies",            "get_anomaly_report"),
    ("POST", "/api/v1/security/verification/request", "request_verification"),
    ("POST", "/api/v1/security/verification/submit",  "submit_verification"),
    ("GET",  "/api/v1/security/verification/{param}", "get_verification_status"),

    # Nostr
    ("POST", "/api/v1/nostr/publish",                 "nostr_publish_skill"),
    ("GET",  "/api/v1/nostr/discover",                "nostr_discover_skills"),
    ("GET",  "/api/v1/nostr/profile",                 "nostr_get_profile"),
    ("GET",  "/api/v1/nostr/relays/status",           "nostr_relay_status"),
]

# Pre-compile patterns: replace {param} with a regex group that matches
# one path segment (no slashes).
_COMPILED_ROUTES: list[tuple[str, re.Pattern, str]] = []
for method, pattern, tool in _ROUTE_TOOL_MAP:
    regex = re.escape(pattern).replace(r"\{param\}", r"[^/]+")
    _COMPILED_ROUTES.append((method, re.compile(f"^{regex}$"), tool))


def _resolve_tool(method: str, path: str) -> str | None:
    """Match a request method + path to a tool name, or None if no match."""
    for route_method, regex, tool in _COMPILED_ROUTES:
        if method == route_method and regex.match(path):
            return tool
    return None


# =============================================================================
# Middleware
# =============================================================================


def _extract_client_id(request: Request) -> str | None:
    """
    Extract a client identifier from the request for per-client rate limiting.

    Uses a SHA-256 hash of the API key so the actual key is never stored
    in Redis or logs.  Returns None if no API key is present (the limiter
    will use a global counter).
    """
    api_key = request.headers.get("x-api-key")
    if not api_key:
        return None
    # Short hash — enough for uniqueness, not reversible
    return hashlib.sha256(api_key.encode()).hexdigest()[:16]


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that enforces per-client, per-tool rate limits on
    REST endpoints.

    Sits early in the middleware stack so rate-limited requests are rejected
    before touching auth, database, or LND. Unmatched routes (health, docs,
    root) pass through without rate limiting.

    Each API key gets its own independent rate limit window — one client
    hitting the limit doesn't affect other clients.
    """

    async def dispatch(self, request: Request, call_next):
        tool = _resolve_tool(request.method, request.url.path)

        if tool is None:
            # Unrecognized route — health, docs, OpenAPI, etc. Pass through.
            return await call_next(request)

        client_id = _extract_client_id(request)

        try:
            rate_limiter.check(tool, client_id=client_id)
        except RateLimitExceeded as e:
            # Extract retry_after from the error message
            retry_after = _extract_retry_after(str(e))
            return JSONResponse(
                status_code=429,
                content={
                    "error": "rate_limit_exceeded",
                    "detail": str(e),
                    "tool": tool,
                },
                headers={
                    "Retry-After": str(retry_after),
                },
            )

        # Attach the resolved tool name to request state so downstream
        # handlers can use it for logging/metrics without re-resolving.
        request.state.rate_limit_tool = tool
        return await call_next(request)


def _extract_retry_after(message: str) -> int:
    """Extract the retry-after seconds from a RateLimitExceeded message."""
    # Message format: "... Try again in Ns."
    match = re.search(r"Try again in (\d+)s", message)
    if match:
        return int(match.group(1))
    return 60  # safe default
