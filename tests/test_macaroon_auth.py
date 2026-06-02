"""Tests for the macaroon-based authorization system."""

import pytest

from conduit.services.macaroon_auth import (
    PROFILES,
    TOOL_PERMISSIONS,
    Permission,
    check_tool_permission,
    derive_macaroon,
    mint_root_macaroon,
    set_active_macaroon,
    verify_macaroon,
)

# ── Permission mappings ───────────────────────────────────────────────


class TestPermissionMappings:
    """Every MCP tool should have a permission mapping."""

    EXPECTED_TOOLS = [
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

    def test_all_tools_have_permissions(self):
        """Every known MCP tool should have a permission entry."""
        for tool in self.EXPECTED_TOOLS:
            assert tool in TOOL_PERMISSIONS, f"No permission mapping for {tool}"

    def test_pay_requires_pay_permission(self):
        """pay_invoice should require lightning:pay, not just read."""
        assert TOOL_PERMISSIONS["pay_invoice"] == Permission.LIGHTNING_PAY

    def test_read_tools_require_read(self):
        """Read-only tools should only need read permission."""
        read_tools = ["get_node_info", "get_balance", "decode_invoice", "check_payment"]
        for tool in read_tools:
            assert TOOL_PERMISSIONS[tool] == Permission.LIGHTNING_READ

    def test_admin_tools_require_admin(self):
        """Admin tools should require security:admin."""
        assert TOOL_PERMISSIONS["create_macaroon"] == Permission.SECURITY_ADMIN


# ── Profile definitions ──────────────────────────────────────────────


class TestProfiles:
    """Pre-defined permission profiles."""

    def test_admin_has_all_permissions(self):
        """Admin profile should include every permission."""
        admin_perms = set(PROFILES["admin"])
        all_perms = set(Permission)
        assert admin_perms == all_perms

    def test_readonly_cannot_pay(self):
        """Readonly profile should not include pay or write permissions."""
        readonly_perms = set(PROFILES["readonly"])
        assert Permission.LIGHTNING_PAY not in readonly_perms
        assert Permission.MARKETPLACE_WRITE not in readonly_perms
        assert Permission.MARKETPLACE_EXECUTE not in readonly_perms

    def test_marketplace_cannot_pay_lightning(self):
        """Marketplace profile should not include lightning:pay."""
        marketplace_perms = set(PROFILES["marketplace"])
        assert Permission.LIGHTNING_PAY not in marketplace_perms

    def test_spending_can_pay(self):
        """Spending profile should include lightning:pay."""
        spending_perms = set(PROFILES["spending"])
        assert Permission.LIGHTNING_PAY in spending_perms

    def test_four_profiles_exist(self):
        """There should be exactly 4 profiles."""
        assert len(PROFILES) == 4
        assert set(PROFILES.keys()) == {"admin", "readonly", "marketplace", "spending"}


# ── Macaroon mint / verify cycle ─────────────────────────────────────


class TestMacaroonCycle:
    """Minting and verifying macaroons."""

    def test_root_macaroon_has_all_permissions(self):
        """Root macaroon should grant every permission."""
        root = mint_root_macaroon()
        perms = verify_macaroon(root)
        assert perms == set(Permission)

    def test_derived_readonly_macaroon(self):
        """A readonly macaroon should only grant read permissions."""
        mac = derive_macaroon(profile="readonly")
        perms = verify_macaroon(mac)
        assert Permission.LIGHTNING_READ in perms
        assert Permission.LIGHTNING_PAY not in perms

    def test_derived_spending_macaroon(self):
        """A spending macaroon should grant pay but not admin."""
        mac = derive_macaroon(profile="spending")
        perms = verify_macaroon(mac)
        assert Permission.LIGHTNING_PAY in perms
        assert Permission.SECURITY_ADMIN not in perms

    def test_custom_permissions(self):
        """Custom permission list should be respected."""
        mac = derive_macaroon(permissions=["lightning:read", "lightning:pay"])
        perms = verify_macaroon(mac)
        assert perms == {Permission.LIGHTNING_READ, Permission.LIGHTNING_PAY}

    def test_invalid_permission_raises(self):
        """Unknown permission strings should raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            derive_macaroon(permissions=["lightning:read", "bogus:permission"])
        assert "bogus:permission" in str(exc_info.value)

    def test_no_profile_or_permissions_raises(self):
        """Calling derive_macaroon with neither arg should raise."""
        with pytest.raises(ValueError):
            derive_macaroon()

    def test_invalid_macaroon_string_raises(self):
        """Garbage input to verify_macaroon should raise ValueError."""
        with pytest.raises(ValueError):
            verify_macaroon("this-is-not-a-macaroon")


# ── Tool permission checking ─────────────────────────────────────────


class TestToolPermissionCheck:
    """Tests for check_tool_permission with active macaroon."""

    def test_admin_allows_everything(self):
        """With admin macaroon, all tools should be allowed."""
        root = mint_root_macaroon()
        set_active_macaroon(root)

        # Should not raise for any tool
        for tool in TOOL_PERMISSIONS:
            check_tool_permission(tool)

    def test_readonly_blocks_pay(self):
        """With readonly macaroon, pay_invoice should be blocked."""
        mac = derive_macaroon(profile="readonly")
        set_active_macaroon(mac)

        with pytest.raises(PermissionError) as exc_info:
            check_tool_permission("pay_invoice")
        assert "lightning:pay" in str(exc_info.value)

    def test_readonly_allows_reads(self):
        """With readonly macaroon, read tools should work."""
        mac = derive_macaroon(profile="readonly")
        set_active_macaroon(mac)

        check_tool_permission("get_balance")
        check_tool_permission("discover_skills")

    def test_unknown_tool_fails_closed(self):
        """Unknown tools must be denied — adding a tool requires updating
        TOOL_PERMISSIONS in the same change."""
        mac = derive_macaroon(profile="readonly")
        set_active_macaroon(mac)

        with pytest.raises(PermissionError) as exc_info:
            check_tool_permission("some_future_tool_v2")
        assert "permission mapping" in str(exc_info.value)

    def test_no_macaroon_fails_closed(self):
        """If no macaroon is set, every tool check must deny."""
        import conduit.services.macaroon_auth as mod
        mod._active_macaroon = None
        mod._active_permissions = None

        with pytest.raises(PermissionError) as exc_info:
            check_tool_permission("get_node_info")
        assert "not initialized" in str(exc_info.value).lower() \
            or "no active" in str(exc_info.value).lower()

    def teardown_method(self):
        """Reset active macaroon after each test."""
        import conduit.services.macaroon_auth as mod
        mod._active_macaroon = None
        mod._active_permissions = None


# ── Privilege-escalation regression test ─────────────────────────────


class TestMacaroonAttenuation:
    """A holder of a derived macaroon must NEVER be able to escalate by
    appending their own permissions caveat. This is the bug class the
    intersection-semantics fix prevents."""

    def test_appended_caveat_cannot_expand_permissions(self):
        """Appending a permissions caveat with extra perms must be a no-op
        (intersection drops any perm not in the original caveat)."""
        import json

        from pymacaroons import Macaroon

        readonly = derive_macaroon(profile="readonly")
        m = Macaroon.deserialize(readonly)
        # Attacker appends a caveat granting admin
        m.add_first_party_caveat(
            f"permissions = {json.dumps(['security:admin', 'lightning:pay'])}"
        )
        tampered = m.serialize()

        granted = verify_macaroon(tampered)
        # readonly perms intersected with {admin, pay} → empty (or only
        # whatever overlap exists; readonly does NOT include admin or pay)
        assert Permission.SECURITY_ADMIN not in granted
        assert Permission.LIGHTNING_PAY not in granted

    def test_appended_caveat_can_restrict_further(self):
        """Legitimate attenuation: appending a NARROWER caveat is fine."""
        import json

        from pymacaroons import Macaroon

        readonly = derive_macaroon(profile="readonly")
        m = Macaroon.deserialize(readonly)
        # Holder restricts down to just lightning:read
        m.add_first_party_caveat(
            f"permissions = {json.dumps(['lightning:read'])}"
        )
        narrowed = m.serialize()

        granted = verify_macaroon(narrowed)
        assert granted == {Permission.LIGHTNING_READ}

    def test_unknown_caveat_rejects_macaroon(self):
        """A macaroon carrying a caveat we don't recognize must NOT be
        treated as if it were unrestricted."""
        from pymacaroons import Macaroon

        readonly = derive_macaroon(profile="readonly")
        m = Macaroon.deserialize(readonly)
        m.add_first_party_caveat("time < 9999999999")
        with pytest.raises(ValueError):
            verify_macaroon(m.serialize())
