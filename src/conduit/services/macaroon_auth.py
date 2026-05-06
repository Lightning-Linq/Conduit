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
    # Security
    "get_spending_status": Permission.SECURITY_READ,
    "get_anomaly_report": Permission.SECURITY_READ,
    "create_macaroon": Permission.SECURITY_ADMIN,
    "list_permissions": Permission.SECURITY_READ,
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


def derive_macaroon(profile: str | None = None, permissions: list[str] | None = None) -> str:
    """
    Derive a scoped macaroon from the root.

    Either specify a profile name ('readonly', 'marketplace', 'spending')
    or a custom list of permission strings.
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

    # Create from root — the caveat restricts permissions
    m = Macaroon(
        location=_LOCATION,
        identifier=f"conduit-{profile or 'custom'}",
        key=_get_secret(),
    )
    m.add_first_party_caveat(f"permissions = {json.dumps(sorted(perms))}")
    return m.serialize()


def verify_macaroon(serialized: str) -> set[Permission]:
    """
    Verify a macaroon and extract its permissions.
    Returns the set of granted permissions.
    Raises ValueError if verification fails.
    """
    try:
        m = Macaroon.deserialize(serialized)
    except Exception as e:
        raise ValueError(f"Invalid macaroon: {e}")

    # Verify signature
    v = Verifier()

    # Extract permissions from caveats
    granted: set[Permission] = set()

    def check_permissions(caveat: str) -> bool:
        if caveat.startswith("permissions = "):
            perms_json = caveat[len("permissions = "):]
            try:
                perms = json.loads(perms_json)
                for p in perms:
                    try:
                        granted.add(Permission(p))
                    except ValueError:
                        pass  # Skip unknown permissions
                return True
            except json.JSONDecodeError:
                return False
        return False

    v.satisfy_general(check_permissions)

    try:
        v.verify(m, _get_secret())
    except Exception as e:
        raise ValueError(f"Macaroon verification failed: {e}")

    return granted


def check_tool_permission(tool_name: str) -> None:
    """
    Check if the active macaroon allows calling the given tool.
    Raises PermissionError if not authorized.
    """
    if _active_permissions is None:
        # No macaroon set — running in unrestricted mode (backward compat)
        return

    required = TOOL_PERMISSIONS.get(tool_name)
    if required is None:
        # Unknown tool — allow (don't break new tools)
        return

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
