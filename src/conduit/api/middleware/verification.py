"""
Verification enforcement middleware — warns or blocks on unverified skills.

Applies to the skill execution REQUEST endpoint. When a consumer requests
execution of an unverified skill, the middleware:

  1. Adds an X-Conduit-Verification-Warning header to the response so the
     consumer's agent can surface the risk to the user.
  2. If the consumer set ?require_verified=true (or the operator configured
     REQUIRE_VERIFIED_SKILLS=true), returns 403 instead of proceeding.

This does NOT block skill discovery or registration — only execution of
unverified skills carries a warning or gate.

This middleware is NOT the whole policy. It sees exactly one path, so the
other execution doors enforce REQUIRE_VERIFIED_SKILLS themselves — the
federation router (peer buyers), the marketplace broker (peer-hosted skills,
which this middleware cannot resolve because it looks for a local Skill row),
and mcp_server (the primary agent interface). All of them share one predicate,
core/verification_policy.py, because they have drifted apart before.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from conduit.core.config import settings
from conduit.core.verification_policy import is_verified_status

# The ONE path this middleware gates: POST /api/v1/marketplace/executions.
#
# Confirm (POST /executions/{id}/confirm) is deliberately NOT gated. By then the
# consumer has already paid the invoice over Lightning; refusing to deliver would
# pocket a settled payment with no refund path, which is strictly worse than
# either allowing it or having blocked the request in the first place. The gate
# belongs before any invoice is minted, and that is where it lives — here, in the
# federation router, and in mcp_server. (This used to be a regex that also matched
# the confirm path but was never applied to it; the exact-path check below was the
# real behavior. The right outcome, so it is now the stated one.)
_EXECUTION_PATH = "/api/v1/marketplace/executions"


class VerificationEnforcementMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that enforces provider verification on execution.

    Injects itself between rate-limiting and routing. For execution
    endpoints, looks up the skill's verification status and either:
      - Adds a warning header (default behavior), or
      - Blocks the request with 403 if enforcement is strict.

    The skill_id comes from the request body (POST /executions with skill_id
    in JSON). Confirmations pass through untouched — the skill was already
    checked at request time, and the consumer has since paid.
    """

    def __init__(self, app, get_session_fn: Callable | None = None):
        super().__init__(app)
        self._get_session = get_session_fn

    async def dispatch(self, request: Request, call_next):
        # Only check POST to execution endpoints
        if request.method != "POST":
            return await call_next(request)

        path = request.url.path

        # Only enforce on new execution requests (not confirm/rate) — see
        # _EXECUTION_PATH for why confirm is out of scope on purpose.
        if path != _EXECUTION_PATH:
            return await call_next(request)

        # Check if enforcement is required (operator config or query param)
        require_verified = settings.require_verified_skills
        if not require_verified:
            # Check query param override
            require_param = request.query_params.get("require_verified", "")
            require_verified = require_param.lower() in ("true", "1", "yes")

        # Read the skill_id from the request body
        skill_id = await self._extract_skill_id(request)
        if not skill_id:
            # Can't determine skill — let the router handle validation
            return await call_next(request)

        # Look up verification status
        verification_status = await self._get_verification_status(skill_id)

        if verification_status is None:
            # Skill not found — let the router 404
            return await call_next(request)

        is_verified = is_verified_status(verification_status)

        if not is_verified and require_verified:
            return JSONResponse(
                status_code=403,
                content={
                    "error": "skill_not_verified",
                    "detail": (
                        f"Skill is '{verification_status}'. Execution of "
                        f"unverified skills is blocked by policy. The provider "
                        f"must complete node or domain verification first."
                    ),
                    "verification_status": verification_status,
                    "skill_id": skill_id,
                },
                headers={
                    "X-Conduit-Verification": verification_status,
                },
            )

        # Proceed with the request, adding a warning header if unverified
        response = await call_next(request)

        if not is_verified:
            response.headers["X-Conduit-Verification-Warning"] = (
                f"Skill is '{verification_status}'. "
                "Provider has not completed verification."
            )
        response.headers["X-Conduit-Verification"] = verification_status

        return response

    async def _extract_skill_id(self, request: Request) -> str | None:
        """Extract skill_id from the JSON request body.

        H11: Uses request.body() instead of request.json() so the raw
        bytes are cached on the Request object. This avoids consuming
        the ASGI receive stream, which would leave downstream handlers
        with an empty body in some BaseHTTPMiddleware configurations.
        """
        try:
            import json
            raw = await request.body()
            body = json.loads(raw)
            return body.get("skill_id")
        except Exception:
            return None

    async def _get_verification_status(self, skill_id: str) -> str | None:
        """Look up a skill's verification status from the database."""
        if not self._get_session:
            return None

        try:
            import uuid

            from sqlalchemy import select

            from conduit.models.skill import Skill

            uid = uuid.UUID(skill_id)
            session_factory = self._get_session

            async with session_factory() as session:
                result = await session.execute(
                    select(Skill.verification_status).where(Skill.id == uid)
                )
                row = result.scalar_one_or_none()
                return row
        except Exception as e:
            print(
                f"[verification-middleware] Could not check skill {skill_id}: {e}",
                file=sys.stderr,
            )
            return None
