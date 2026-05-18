"""
L402 FastAPI middleware — adds HTTP 402 payment-gated authentication.

How it works:
  1. Request arrives at a protected endpoint.
  2. Middleware checks for Authorization: L402 <macaroon>:<preimage> header.
     - If present and valid → request passes through (authenticated via L402).
     - If present but invalid → 401 with error details.
  3. If no L402 header, checks for X-API-Key header (backward compat).
     - If present and valid → request passes through (API key auth).
  4. If neither credential is present:
     - If L402 is enabled → 402 with WWW-Authenticate header + invoice.
     - If L402 is disabled → 401 "API key required" (existing behavior).

This means L402 and API key auth coexist. Operators who don't enable L402
get the exact same behavior as before. Operators who enable it get a
pay-per-request alternative that doesn't require pre-shared keys.

The middleware is applied as a Starlette middleware, not a FastAPI dependency,
so it intercepts requests before routing and can set the 402 response with
the correct headers regardless of which router handles the endpoint.
"""

from __future__ import annotations

import sys
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from conduit.core.config import settings
from conduit.services.l402 import (
    L402Challenge,
    create_l402_challenge,
    format_www_authenticate,
    parse_l402_header,
    verify_l402,
)


class L402Middleware(BaseHTTPMiddleware):
    """
    Starlette middleware that implements the L402 payment-gate protocol.

    When L402 is enabled (L402_ENABLED=true), endpoints that don't match
    a free-route prefix will require either:
      - A valid X-API-Key header (existing auth), OR
      - A valid Authorization: L402 <macaroon>:<preimage> header.

    If neither is present, the middleware returns 402 Payment Required
    with a freshly minted invoice.
    """

    def __init__(self, app, get_lnd_fn: Callable | None = None):
        """
        Args:
            app: the ASGI application.
            get_lnd_fn: callable that returns an LndClient. Injected so the
                        middleware doesn't import the LND singleton at module
                        level (avoids circular imports and makes testing easier).
        """
        super().__init__(app)
        self._get_lnd = get_lnd_fn

    async def dispatch(self, request: Request, call_next):
        # If L402 is not enabled, pass through — existing API key auth handles it.
        if not settings.l402_enabled:
            return await call_next(request)

        # Check if this route is free (no payment required).
        path = request.url.path
        if self._is_free_route(path):
            return await call_next(request)

        # ── Try L402 credential first ───────────────────────────────
        auth_header = request.headers.get("authorization", "")
        l402_cred = parse_l402_header(auth_header)

        if l402_cred is not None:
            result = verify_l402(l402_cred)
            if result.valid:
                # Attach L402 metadata to request state for downstream use.
                request.state.l402_payment_hash = result.payment_hash
                request.state.l402_resource = result.resource
                request.state.auth_method = "l402"
                return await call_next(request)
            else:
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "l402_invalid",
                        "detail": result.error,
                    },
                )

        # ── Try API key (backward compat) ───────────────────────────
        api_key = request.headers.get("x-api-key", "")
        if api_key:
            # Let the existing verify_api_key dependency handle validation.
            # We just pass through here; the router dependency will 401 if bad.
            return await call_next(request)

        # ── No credentials at all → issue 402 challenge ─────────────
        return await self._issue_challenge(request)

    async def _issue_challenge(self, request: Request) -> JSONResponse:
        """Return a 402 Payment Required response with L402 challenge."""
        if self._get_lnd is None:
            return JSONResponse(
                status_code=503,
                content={"error": "L402 enabled but LND client not configured"},
            )

        try:
            lnd = self._get_lnd()
        except Exception as e:
            print(f"[l402] Could not connect to LND: {e}", file=sys.stderr)
            return JSONResponse(
                status_code=503,
                content={"error": "Lightning node unavailable for L402 challenge"},
            )

        # Determine price for this endpoint.
        price = self._get_price_for_route(request.url.path)

        # Determine resource scope from the route.
        resource = self._route_to_resource(request.url.path)

        try:
            challenge = create_l402_challenge(
                lnd,
                amount_sats=price,
                memo=f"Conduit L402: {request.method} {request.url.path}",
                resource=resource,
            )
        except Exception as e:
            print(f"[l402] Failed to create challenge: {e}", file=sys.stderr)
            return JSONResponse(
                status_code=503,
                content={"error": "Failed to generate L402 invoice"},
            )

        return JSONResponse(
            status_code=402,
            content={
                "error": "payment_required",
                "message": (
                    "Pay the Lightning invoice, then retry with "
                    "Authorization: L402 <macaroon>:<preimage>"
                ),
                "macaroon": challenge.macaroon,
                "invoice": challenge.invoice,
                "payment_hash": challenge.payment_hash,
                "amount_sats": challenge.amount_sats,
                "expires_at": challenge.expires_at,
            },
            headers={
                "WWW-Authenticate": format_www_authenticate(challenge),
            },
        )

    def _is_free_route(self, path: str) -> bool:
        """Check if a route is free (no L402 or API key required)."""
        for prefix in settings.l402_free_route_list:
            # Exact match for "/" to avoid matching everything
            if prefix == "/":
                if path == "/":
                    return True
            elif path.startswith(prefix):
                return True
        return False

    def _get_price_for_route(self, path: str) -> int:
        """
        Determine the price in sats for an endpoint.

        Currently uses the global default. Future: per-skill pricing
        based on the skill's registered price_sats.
        """
        # Skill execution endpoints could use the skill's own price.
        # For now, use the global default.
        return settings.l402_default_price_sats

    def _route_to_resource(self, path: str) -> str | None:
        """
        Map a route path to an L402 resource scope.

        This constrains the minted token so it can only be used for the
        resource the client originally requested — prevents a token bought
        for GET /balance being replayed on POST /payments.
        """
        # Strip /api/v1 prefix for cleaner resource names
        clean = path.removeprefix("/api/v1")

        if clean.startswith("/lightning"):
            return "lightning"
        elif clean.startswith("/marketplace"):
            return "marketplace"
        elif clean.startswith("/security"):
            return "security"
        elif clean.startswith("/nostr"):
            return "nostr"

        return None
