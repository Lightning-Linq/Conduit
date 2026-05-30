"""
Verification enforcement middleware — warns or blocks on unverified skills.

Applies to skill execution endpoints. When a consumer requests execution
of an unverified skill, the middleware:

  1. Adds an X-Conduit-Verification-Warning header to the response so the
     consumer's agent can surface the risk to the user.
  2. If the consumer set ?require_verified=true (or the operator configured
     REQUIRE_VERIFIED_SKILLS=true), returns 403 instead of proceeding.

This does NOT block skill discovery or registration — only execution of
unverified skills carries a warning or gate.
"""

from __future__ import annotations

import re
import sys
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from conduit.core.config import settings


# Execution endpoints that should check verification status.
# Match: POST /api/v1/marketplace/executions (request execution)
# Match: POST /api/v1/marketplace/executions/{id}/confirm
_EXECUTION_RE = re.compile(
    r"^/api/v1/marketplace/executions(?:/[^/]+/confirm)?$"
)


class VerificationEnforcementMiddleware(BaseHTTPMiddleware):
    """
    Starlette middleware that enforces provider verification on execution.

    Injects itself between rate-limiting and routing. For execution
    endpoints, looks up the skill's verification status and either:
      - Adds a warning header (default behavior), or
      - Blocks the request with 403 if enforcement is strict.

    The skill_id comes from the request body for new executions (POST
    /executions with skill_id in JSON) or from the execution record for
    confirmations. For confirmations we pass through since the skill was
    already checked at request time.
    """

    def __init__(self, app, get_session_fn: Callable | None = None):
        super().__init__(app)
        self._get_session = get_session_fn

    async def dispatch(self, request: Request, call_next):
        # Only check POST to execution endpoints
        if request.method != "POST":
            return await call_next(request)

        path = request.url.path

        # Only enforce on new execution requests (not confirm/rate)
        if path != "/api/v1/marketplace/executions":
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

        is_verified = verification_status in ("node_verified", "domain_verified", "fully_verified")

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
