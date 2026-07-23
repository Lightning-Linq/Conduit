"""Seller subscription — model, activity logic, and listing quota gate.

Free tier: up to `free_tier_max_active_skills` active listings with the
standard platform fee. Pro: unlimited active listings while paid_until is
in the future. Quotas gate REGISTRATION (and reactivation via the sweep);
they are enforcement on the LL-hosted node, advisory on self-hosted ones
(provider_name is unauthenticated — out of scope here).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conduit.core.config import settings
from conduit.models.subscription import ProviderSubscription
from conduit.services import subscription as sub_svc


class TestTierConfig:
    """Starter is the middle tier: 15 listings for 2,500 sats/mo (25k/yr).
    Pro prices keep their existing (unprefixed) setting names."""

    def test_starter_listing_cap(self):
        assert settings.subscription_starter_max_active_skills == 15

    def test_starter_prices(self):
        assert settings.subscription_starter_price_sats_monthly == 2_500
        assert settings.subscription_starter_price_sats_yearly == 25_000

    def test_pro_prices_unchanged(self):
        assert settings.subscription_price_sats_monthly == 10_000
        assert settings.subscription_price_sats_yearly == 100_000


class TestModel:
    def test_table_matches_migration(self):
        assert ProviderSubscription.__tablename__ == "provider_subscriptions"
        cols = {c.name for c in ProviderSubscription.__table__.columns}
        assert {
            "id",
            "provider_name",
            "tier",
            "paid_until",
            "invoice_payment_hash",
            "invoice_payment_request",
            "invoice_amount_sats",
            "invoice_period",
            "invoice_tier",
            "invoice_source",
            "created_at",
            "updated_at",
        } <= cols


class TestIsSubscriptionActive:
    def test_none_subscription_inactive(self):
        assert sub_svc.is_subscription_active(None) is False

    def test_never_paid_inactive(self):
        sub = MagicMock(paid_until=None)
        assert sub_svc.is_subscription_active(sub) is False

    def test_expired_inactive(self):
        sub = MagicMock(paid_until=datetime.now(UTC) - timedelta(days=1))
        assert sub_svc.is_subscription_active(sub) is False

    def test_future_active(self):
        sub = MagicMock(paid_until=datetime.now(UTC) + timedelta(days=1))
        assert sub_svc.is_subscription_active(sub) is True


class TestActiveListingCap:
    """Cap resolves from the ACTIVE tier: Free 3, Starter 15, Pro unlimited."""

    def _active(self, tier: str) -> MagicMock:
        return MagicMock(tier=tier, paid_until=datetime.now(UTC) + timedelta(days=1))

    def test_none_is_free_cap(self):
        assert sub_svc.active_listing_cap(None) == 3

    def test_expired_is_free_cap(self):
        sub = MagicMock(tier="pro", paid_until=datetime.now(UTC) - timedelta(days=1))
        assert sub_svc.active_listing_cap(sub) == 3

    def test_active_starter_cap(self):
        assert sub_svc.active_listing_cap(self._active("starter")) == 15

    def test_active_pro_unlimited(self):
        assert sub_svc.active_listing_cap(self._active("pro")) is None

    def test_active_unknown_tier_unlimited(self):
        # Legacy/default rows carry tier="pro"; any other active tier is
        # treated as unlimited rather than silently downgraded.
        assert sub_svc.active_listing_cap(self._active("legacy")) is None


def _session(active_count: int, sub: object | None) -> AsyncMock:
    """Stub session: first execute -> active-skill count, second -> sub row."""
    session = AsyncMock()
    count_result = MagicMock()
    count_result.scalar.return_value = active_count
    sub_result = MagicMock()
    sub_result.scalar_one_or_none.return_value = sub
    session.execute = AsyncMock(side_effect=[count_result, sub_result])
    return session


class TestCanRegisterSkill:
    @pytest.mark.asyncio
    async def test_under_free_quota_allowed(self):
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            ok, reason = await sub_svc.can_register_skill(_session(2, None), "acme")
        assert ok is True
        assert reason is None

    @pytest.mark.asyncio
    async def test_at_free_quota_blocked(self):
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            ok, reason = await sub_svc.can_register_skill(_session(3, None), "acme")
        assert ok is False
        assert "3" in reason  # mentions the quota
        # Free-tier block points at both paid tiers.
        assert "Starter" in reason and "Pro" in reason

    @pytest.mark.asyncio
    async def test_starter_under_cap_allowed(self):
        sub = MagicMock(
            tier="starter", paid_until=datetime.now(UTC) + timedelta(days=30)
        )
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            ok, _ = await sub_svc.can_register_skill(_session(14, sub), "acme")
        assert ok is True

    @pytest.mark.asyncio
    async def test_starter_at_cap_blocked_points_to_pro(self):
        sub = MagicMock(
            tier="starter", paid_until=datetime.now(UTC) + timedelta(days=30)
        )
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            ok, reason = await sub_svc.can_register_skill(_session(15, sub), "acme")
        assert ok is False
        assert "15" in reason and "Pro" in reason

    @pytest.mark.asyncio
    async def test_pro_unlimited_allowed_high_count(self):
        sub = MagicMock(tier="pro", paid_until=datetime.now(UTC) + timedelta(days=30))
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            ok, _ = await sub_svc.can_register_skill(_session(500, sub), "acme")
        assert ok is True

    @pytest.mark.asyncio
    async def test_at_quota_with_active_subscription_allowed(self):
        sub = MagicMock(paid_until=datetime.now(UTC) + timedelta(days=30))
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            ok, _ = await sub_svc.can_register_skill(_session(7, sub), "acme")
        assert ok is True

    @pytest.mark.asyncio
    async def test_lapsed_subscription_blocked(self):
        sub = MagicMock(paid_until=datetime.now(UTC) - timedelta(days=1))
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            ok, _ = await sub_svc.can_register_skill(_session(3, sub), "acme")
        assert ok is False

    @pytest.mark.asyncio
    async def test_flag_off_unrestricted(self):
        session = AsyncMock()  # must not even be queried
        with patch.object(sub_svc.settings, "subscription_enabled", False):
            ok, reason = await sub_svc.can_register_skill(session, "acme")
        assert ok is True
        assert reason is None
        session.execute.assert_not_called()


class TestLapseSweep:
    """Deactivate-overflow on lapse: newest listings beyond the free quota
    are hidden until renewal; subscribed/free-tier providers are untouched."""

    @staticmethod
    def _sweep_session(over_quota, sub, overflow_ids):
        """execute() sequence: over-quota providers -> sub row -> overflow ids
        -> (update). Extra calls just get an empty result."""
        session = AsyncMock()
        over_result = MagicMock()
        over_result.all.return_value = over_quota
        sub_result = MagicMock()
        sub_result.scalar_one_or_none.return_value = sub
        ids_result = MagicMock()
        ids_result.all.return_value = [(i,) for i in overflow_ids]
        update_result = MagicMock()
        session.execute = AsyncMock(
            side_effect=[over_result, sub_result, ids_result, update_result]
        )
        return session

    @pytest.mark.asyncio
    async def test_lapsed_provider_overflow_deactivated(self):
        session = self._sweep_session([("acme",)], None, ["id4", "id5"])
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            n = await sub_svc.enforce_listing_quotas(session)
        assert n == 2
        # The UPDATE targeted exactly the overflow ids.
        update_stmt = session.execute.call_args_list[3].args[0]
        assert "UPDATE skills" in str(update_stmt)

    @pytest.mark.asyncio
    async def test_active_subscription_untouched(self):
        from datetime import UTC, datetime, timedelta

        sub = MagicMock(paid_until=datetime.now(UTC) + timedelta(days=5))
        session = self._sweep_session([("acme",)], sub, [])
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            n = await sub_svc.enforce_listing_quotas(session)
        assert n == 0
        assert len(session.execute.call_args_list) == 2  # no overflow queries

    @pytest.mark.asyncio
    async def test_no_over_quota_providers_noop(self):
        session = self._sweep_session([], None, [])
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            n = await sub_svc.enforce_listing_quotas(session)
        assert n == 0

    @pytest.mark.asyncio
    async def test_starter_over_cap_trimmed_to_15(self):
        sub = MagicMock(
            tier="starter", paid_until=datetime.now(UTC) + timedelta(days=5)
        )
        session = self._sweep_session([("acme",)], sub, ["id16", "id17"])
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            n = await sub_svc.enforce_listing_quotas(session)
        assert n == 2  # hid the two listings beyond the Starter cap
        update_stmt = session.execute.call_args_list[3].args[0]
        assert "UPDATE skills" in str(update_stmt)

    @pytest.mark.asyncio
    async def test_starter_under_cap_untouched(self):
        # Over the free quota (>3) but within Starter's 15 — no overflow rows.
        sub = MagicMock(
            tier="starter", paid_until=datetime.now(UTC) + timedelta(days=5)
        )
        session = self._sweep_session([("acme",)], sub, [])
        with patch.object(sub_svc.settings, "subscription_enabled", True):
            n = await sub_svc.enforce_listing_quotas(session)
        assert n == 0


class TestReactivateUpToCap:
    """Renewal restores hidden listings, but never above the tier cap."""

    @pytest.mark.asyncio
    async def test_unlimited_reactivates_all(self):
        session = AsyncMock()
        session.execute = AsyncMock(return_value=MagicMock(rowcount=4))
        n = await sub_svc.reactivate_up_to_cap(session, "acme", None)
        assert n == 4
        assert len(session.execute.call_args_list) == 1  # single bulk UPDATE

    @pytest.mark.asyncio
    async def test_bounded_fills_only_open_slots(self):
        count_result = MagicMock()
        count_result.scalar.return_value = 13  # 2 slots open under cap 15
        ids_result = MagicMock()
        ids_result.all.return_value = [("id14",), ("id15",)]
        session = AsyncMock()
        session.execute = AsyncMock(
            side_effect=[count_result, ids_result, MagicMock()]
        )
        n = await sub_svc.reactivate_up_to_cap(session, "acme", 15)
        assert n == 2

    @pytest.mark.asyncio
    async def test_at_cap_reactivates_nothing(self):
        count_result = MagicMock()
        count_result.scalar.return_value = 15  # already at cap
        session = AsyncMock()
        session.execute = AsyncMock(side_effect=[count_result])
        n = await sub_svc.reactivate_up_to_cap(session, "acme", 15)
        assert n == 0
        assert len(session.execute.call_args_list) == 1  # only the count query

    @pytest.mark.asyncio
    async def test_flag_off_no_queries(self):
        session = AsyncMock()
        with patch.object(sub_svc.settings, "subscription_enabled", False):
            n = await sub_svc.enforce_listing_quotas(session)
        assert n == 0
        session.execute.assert_not_called()


class TestMcpRegisterGate:
    """The MCP register path honors the same quota gate as REST."""

    @pytest.mark.asyncio
    async def test_mcp_register_blocked_over_quota(self):
        import conduit.mcp_server as mcp

        session = AsyncMock()
        ctx = AsyncMock()
        ctx.__aenter__.return_value = session
        ctx.__aexit__.return_value = False
        factory = MagicMock(return_value=ctx)

        with patch.object(mcp, "async_session_factory", factory), patch.object(
            mcp,
            "can_register_skill",
            AsyncMock(return_value=(False, "Free tier allows 3 active listings")),
        ):
            result = await mcp._register_skill(
                {
                    "name": "n",
                    "description": "d",
                    "category": "general",
                    "price_sats": 100,
                    "provider_name": "acme",
                    "provider_lightning_address": "a@b.c",
                }
            )
        assert "Free tier" in result[0].text
        session.add.assert_not_called()  # blocked before any insert
