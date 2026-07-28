"""What REQUIRE_VERIFIED_SKILLS considers verified — one definition, every door.

Three front doors decide whether a skill may be executed: the REST middleware
(local buyers), the federation router (peer buyers), and the MCP server (the
primary agent interface). Each owns its own refusal shape — a JSONResponse, an
HTTPException, a TextContent — but the *predicate* has to be identical, because
they have already drifted once: for a long stretch only the REST middleware read
the flag at all, so an MCP-only operator who set REQUIRE_VERIFIED_SKILLS=true got
no enforcement whatsoever.

Deliberately dependency-free (no settings, no models): every door reads the flag
itself, which keeps the per-door test seams — and keeps this importable from
middleware without dragging the API layer in behind it.

The badges come from services/provider_verification.py. "expired" and "unverified"
are NOT verified: a lapsed badge must stop selling, not coast.
"""

from __future__ import annotations

VERIFIED_STATUSES: tuple[str, ...] = (
    "node_verified",
    "domain_verified",
    "fully_verified",
)


def is_verified_status(status: str | None) -> bool:
    """True if this verification_status satisfies REQUIRE_VERIFIED_SKILLS.

    Unknown, missing, and "expired" statuses are all unverified — fail closed,
    so a new badge value never becomes an accidental bypass.
    """
    return status in VERIFIED_STATUSES
