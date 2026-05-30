"""
Macaroon-based authorization for Conduit MCP tools.

Defines permission scopes, mints root macaroons from the API key,
derives restricted macaroons with caveats, and verifies permissions
on incoming tool calls.

Permission scopes:
- lightning:read     — get_node_info, get_balance, decode_invoice, check_payment
- lightning:invoice  — create_invoice
- lightning:pay      — pay_invoice
- marketplace:read   — discover_skills, get_skill_details
- marketplace:write  — register_skill
- marketplace:execute — request_skill_execution, submit_rating
- security:read      — get_spending_status
- security:admin     — manage macaroons (create_macaroon, list_permissions)
"""

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum

from pymacaroons import Macaroon, Verifier

from conduit.core.config import settings


# =============================================================================
# Permission Scopes
# =============================================================================

class Permission(str, Enum):
    """All available permission scopes."""
    LIGHTNING_READ = "lightning:read"
    LIGHTNING_INVOICE = "lightning:invoice"
    LIGHTNING_PAY = "lightning:pay"
    MARKETPLACE_READ = "marketplace:read"
    MARKETPLACE_WRITE = "marketplace:write"
    MARKETPLACE_EXECUTE = "marketplace:execute"
    NOSTR_READ = "nostr:read"
    NOSTR_WRITE = "nostr:write"
    SECURITY_READ = "security:read"
    SECURITY_ADMIN = "security:admin"


# Which permissions each tool requires
TOOL_PERMISSIONS: dict[str, Permission] = {
    # Lightning
    "get_node_info": Permission.LIGHTNING_READ,
    "get_balance": Permission.LIGHTNING_READ,
    "decode_invoice": Permission.LIGHTNING_READ,
    "check_payment": Permission.LIGHTNING_READ,
    "create_invoice": Permission.LIGHTNING_INVOICE,
    "pay_invoice": Permission.LIGHTNING_PAY,
    # Marketplace
    "discover_skills": Permission.MARKETPLACE_READ,
    "get_skill_details": Permission.MARKETPLACE_READ,
    "register_skill": Permission.MARKETPLACE_WRITE,
    "request_skill_execution": Permission.MARKETPLACE_EXECUTE,
    "confirm_skill_execution": Permission.MARKETPLACE_EXECUTE,
    "submit_rating": Permission.MARKETPLACE_EXECUTE,
    # Verification
    "request_verification": Permission.MARKETPLACE_WRITE,
    "submit_verification": Permission.MARKETPLACE_WRITE,
    "get_verification_status": Permission.MARKETPLACE_READ,
    # Nostr
    "nostr_publish_skill": Permission.NOSTR_WRITE,
    "nostr_discover_skills": Permission.NOSTR_READ,
    "nostr_get_profile": Permission.NOSTR_READ,
    "nostr_relay_status": Permission.NOSTR_READ,
    # Security
    "get_spending_status": Permission.SECURITY_READ,
    "get_anomaly_report": Permission.SECURITY_READ,
    "create_macaroon": Permission.SECURITY_ADMIN,
    "list_permissions": Permission.SECURITY_READ,
    # L402
    "create_l402_token": Permission.LIGHTNING_INVOICE,
    "verify_l402_token": Permission.SECURITY_READ,
    "get_l402_status": Permission.SECURITY_READ,
}

# Pre-defined permission profiles
PROFILES: dict[str, list[Permission]] = {
    "admin": list(Permission),  # all permissions
    "readonly": [
        Permission.LIGHTNING_READ,
        Permission.MARKETPLACE_READ,
        Permission.SECURITY_READ,
    ],
    "marketplace": [
        Permission.MARKETPLACE_READ,
        Permission.MARKETPLACE_WRITE,
        Permission.MARKETPLACE_EXECUTE,
        Permission.NOSTR_READ,
        Permission.NOSTR_WRITE,
        Permission.SECURITY_READ,
    ],
    "spending": [
        Permission.LIGHTNING_READ,
        Permission.LIGHTNING_INVOICE,
        Permission.LIGHTNING_PAY,
        Permission.MARKETPLACE_READ,
        Permission.MARKETPLACE_EXECUTE,
        Permission.SECURITY_READ,
    ],
}


# =============================================================================
# Macaroon Service
# =============================================================================

# Location identifier for our macaroons
_LOCATION = "conduit-mcp"

# The active macaroon for the current session (set on startup)
_active_macaroon: Macaroon | None = None
_active_permissions: set[Permission] | None = None


def _get_secret() -> str:
    """Derive the macaroon secret from the API key."""
    # Hash the API key to produce a fixed-length secret
    return hashlib.sha256(settings.conduit_api_key.encode()).hexdigest()


def mint_root_macaroon() -> str:
    """
    Create a root macaroon with all permissions (admin profile).
    Returns the serialized macaroon string.
    """
    m = Macaroon(
        location=_LOCATION,
        identifier="conduit-root",
        key=_get_secret(),
    )
    # Add all permissions as a single caveat
    perms = [p.value for p in Permission]
    m.add_first_party_caveat(f"permissions = {json.dumps(perms)}")
    return m.serialize()


def derive_macaroon(
    profile: str | None = None,
    permissions: list[str] | None = None,
    caller_permissions: list[str] | None = None,
) -> str:
    """
    Derive a scoped macaroon from the root.

    Either specify a profile name ('readonly', 'marketplace', 'spending')
    or a custom list of permission strings.

    M3: If caller_permissions is provided, the derived macaroon's
    permissions are intersected so a caller can never mint tokens
    with more permissions than they currently hold.
    """
    if profile and profile in PROFILES:
        perms = [p.value for p in PROFILES[profile]]
    elif permissions:
        # Validate all permissions
        valid = {p.value for p in Permission}
        for p in permissions:
            if p not in valid:
                raise ValueError(f"Unknown permission: {p}. Valid: {sorted(valid)}")
        perms = permissions
    else:
        raise ValueError(f"Specify a profile ({', '.join(PROFILES.keys())}) or a list of permissions")

    # M3: Intersect with caller's permissions if provided
    if caller_permissions is not None:
        perms = [p for p in perms if p in caller_permissions]
        if not perms:
            raise ValueError("Derived macaroon would have no permissions (caller lacks requested permissions)")

    # Create from root — the caveat restricts permissions
    m = Macaroon(
        location=_LOCATION,
        identifier=f"conduit-{profile or 'custom'}",
        key=_get_secret(),
    )
    m.add_first_party_caveat(f"permissions = {json.dumps(sorted(perms))}")
    return m.serialize()


_PERMS_PREFIX = "permissions = "


def _parse_permission_caveat(caveat_id: str) -> set[Permission] | None:
    """
    Parse a `permissions = [...]` caveat into a set of Permission values.
    Returns None for caveats that aren't a permissions caveat or are malformed.
    Unknown permission strings are silently dropped (caveat is then a no-op,
    not a free pass — see verify_macaroon for intersection semantics).
    """
    if not caveat_id.startswith(_PERMS_PREFIX):
        return None
    try:
        perms = json.loads(caveat_id[len(_PERMS_PREFIX):])
    except json.JSONDecodeError:
        return None
    if not isinstance(perms, list):
        return None
    out: set[Permission] = set()
    valid = {p.value for p in Permission}
    for p in perms:
        if isinstance(p, str) and p in valid:
            out.add(Permission(p))
    return out


def verify_macaroon(serialized: str) -> set[Permission]:
    """
    Verify a macaroon and extract its granted permissions.

    Semantics (important): multiple `permissions = [...]` caveats are
    INTERSECTED, not unioned. In the macaroon protocol any holder can
    append first-party caveats without the secret — that's how attenuation
    works. If we *unioned* caveats, a holder of a `readonly` macaroon could
    append a caveat like `permissions = ["security:admin"]` and the verifier
    would grant the union (escalation). Intersection means appended caveats
    can only ever restrict, never widen.

    A macaroon with zero permission caveats grants nothing.

    Raises ValueError if the macaroon is malformed or the signature is invalid.
    """
    try:
        m = Macaroon.deserialize(serialized)
    except Exception as e:
        raise ValueError(f"Invalid macaroon: {e}")

    # Collect every permission caveat and intersect.
    perm_sets: list[set[Permission]] = []
    has_unrecognized_caveat = False
    for caveat in m.caveats:
        # Third-party caveats aren't supported here; reject them so we don't
        # ever vouch for a token that depends on an external discharge we
        # never check.
        if not caveat.first_party():
            raise ValueError("Third-party caveats are not supported")
        cid = caveat.caveat_id
        parsed = _parse_permission_caveat(cid)
        if parsed is None:
            # Some non-permissions caveat we don't understand. Treat the
            # macaroon as inert rather than silently letting it through.
            has_unrecognized_caveat = True
            continue
        perm_sets.append(parsed)

    if has_unrecognized_caveat:
        raise ValueError("Macaroon contains caveats this verifier does not understand")

    # Verify the cryptographic chain. The predicate must satisfy every
    # caveat we collected; since we've validated their shape above, any
    # well-formed permissions caveat passes here.
    v = Verifier()
    v.satisfy_general(lambda c: c.startswith(_PERMS_PREFIX))
    try:
        v.verify(m, _get_secret())
    except Exception as e:
        raise ValueError(f"Macaroon verification failed: {e}")

    if not perm_sets:
        return set()

    granted = perm_sets[0]
    for ps in perm_sets[1:]:
        granted &= ps
    return granted


def check_tool_permission(tool_name: str) -> None:
    """
    Check if the active macaroon allows calling the given tool.
    Raises PermissionError if not authorized.

    Fails closed: if no macaroon has been initialized this is an
    incorrectly-started server, not a license to skip auth. Callers
    must initialize_root_session() (or set_active_macaroon) at startup.
    """
    if _active_permissions is None:
        raise PermissionError(
            "No active macaroon — server is not initialized. "
            "Call initialize_root_session() on startup."
        )

    required = TOOL_PERMISSIONS.get(tool_name)
    if required is None:
        # Unknown tool name. Fail closed — if you add a new tool, add it
        # to TOOL_PERMISSIONS in the same change.
        raise PermissionError(
            f"Tool '{tool_name}' has no permission mapping; "
            f"add it to TOOL_PERMISSIONS before exposing it."
        )

    if required not in _active_permissions:
        raise PermissionError(
            f"Tool '{tool_name}' requires permission '{required.value}' "
            f"which is not granted by the current macaroon."
        )


def set_active_macaroon(serialized: str) -> set[Permission]:
    """
    Set the active macaroon for this session.
    Returns the granted permissions.
    """
    global _active_macaroon, _active_permissions
    permissions = verify_macaroon(serialized)
    _active_macaroon = Macaroon.deserialize(serialized)
    _active_permissions = permissions
    return permissions


def get_active_permissions() -> set[Permission] | None:
    """Get permissions from the active macaroon, or None if no macaroon is set."""
    return _active_permissions


def initialize_root_session() -> str:
    """
    Initialize the session with a root (admin) macaroon.
    Called on MCP server startup.
    Returns the serialized root macaroon.
    """
    root = mint_root_macaroon()
    set_active_macaroon(root)
    return root
