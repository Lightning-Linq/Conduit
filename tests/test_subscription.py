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

from conduit.models.subscription import ProviderSubscription
from conduit.services import subscription as sub_svc


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
