"""REQUIRE_VERIFIED_SKILLS — one policy, every front door.

The flag was read in exactly one place: VerificationEnforcementMiddleware, which
matches the single path /api/v1/marketplace/executions. MCP is the primary agent
interface (CLAUDE.md), so an operator running MCP-only who set
REQUIRE_VERIFIED_SKILLS=true got no enforcement whatsoever — the flag silently did
nothing. Federation #3 closed the same hole on the cross-node endpoint; this closes
it on MCP and pins the shared predicate all three doors call, so they cannot drift
apart again.

The gate lives at REQUEST time, before any invoice is minted, and deliberately NOT
at confirm time: by then the buyer has already paid, and refusing delivery would
take their money and give nothing back. That decision is pinned here too.
"""

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from conduit.core.config import settings
from conduit.core.verification_policy import VERIFIED_STATUSES, is_verified_status


def _session_factory(session):
    """A factory whose `async with factory() as s:` yields ``session``."""
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    return MagicMock(return_value=ctx)


def _session_seq(values):
    """An AsyncSession stub whose successive queries resolve to ``values``."""
    session = AsyncMock()
    results = []
    for v in values:
        r = MagicMock()
        r.scalar_one_or_none.return_value = v
        results.append(r)
    session.execute = AsyncMock(side_effect=results)
    return session


def _skill(verification_status="unverified", price_sats=1000, **kw):
    from conduit.models.skill import Skill

    return Skill(
        id=uuid.uuid4(),
        provider_name="acme",
        name="Test Skill",
        description="d",
        category="data",
        price_sats=price_sats,
        is_active=True,
        verification_status=verification_status,
        **kw,
    )


class TestSharedPredicate:
    """One definition of 'verified', imported by every door."""

    def test_the_three_verified_badges_pass(self):
        assert all(is_verified_status(s) for s in VERIFIED_STATUSES)
        assert set(VERIFIED_STATUSES) == {
            "node_verified",
            "domain_verified",
            "fully_verified",
        }

    @pytest.mark.parametrize("status", ["unverified", "expired", "pending", "", None])
    def test_everything_else_fails(self, status):
        # 'expired' matters most: a lapsed badge must not keep selling.
        assert is_verified_status(status) is False

    def test_every_door_calls_the_same_predicate(self):
        """No local copies. A second tuple is how the doors drifted last time."""
        import conduit.api.middleware.verification as mw
        import conduit.api.routers.federation as fed
        import conduit.mcp_server as mcp

        assert mw.is_verified_status is is_verified_status
        assert fed.is_verified_status is is_verified_status
        assert mcp.is_verified_status is is_verified_status


class TestMcpRequestPath:
    """The gap this task closes: MCP never consulted the flag."""

    @pytest.fixture
    def wallet(self):
        w = MagicMock()
        w.create_invoice.side_effect = [
            MagicMock(payment_request="lnbc-fee", payment_hash="d" * 64),
            MagicMock(payment_request="lnbc-provider", payment_hash="a" * 64),
        ]
        return w

    async def _run(self, monkeypatch, skill, wallet, session):
        import conduit.mcp_server as mcp

        async def found(_session, _skill_id):
            return skill

        monkeypatch.setattr(mcp, "_find_skill_by_id", found)
        monkeypatch.setattr(mcp, "async_session_factory", _session_factory(session))
        monkeypatch.setattr(mcp, "get_lnd", lambda: wallet)
        return await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})

    async def test_unverified_skill_is_refused_before_any_invoice(
        self, monkeypatch, wallet
    ):
        monkeypatch.setattr(settings, "require_verified_skills", True)
        session = AsyncMock()

        out = await self._run(monkeypatch, _skill("unverified"), wallet, session)

        text = out[0].text
        assert "refused" in text.lower()
        assert "unverified" in text
        wallet.create_invoice.assert_not_called()
        session.add.assert_not_called()
        session.commit.assert_not_called()

    async def test_an_expired_badge_stops_selling(self, monkeypatch, wallet):
        """Verification expiry is enforcement, not decoration."""
        monkeypatch.setattr(settings, "require_verified_skills", True)
        session = AsyncMock()

        out = await self._run(monkeypatch, _skill("expired"), wallet, session)

        assert "refused" in out[0].text.lower()
        wallet.create_invoice.assert_not_called()

    async def test_a_verified_skill_still_sells(self, monkeypatch, wallet):
        monkeypatch.setattr(settings, "require_verified_skills", True)

        out = await self._run(
            monkeypatch, _skill("node_verified"), wallet, AsyncMock()
        )

        assert "Skill Execution Requested" in out[0].text
        assert wallet.create_invoice.call_count == 2

    async def test_default_posture_is_unchanged(self, monkeypatch, wallet):
        """Policy off (the default): unverified skills stay sellable."""
        monkeypatch.setattr(settings, "require_verified_skills", False)

        out = await self._run(monkeypatch, _skill("unverified"), wallet, AsyncMock())

        assert "Skill Execution Requested" in out[0].text

    async def test_the_mcp_tool_refuses_with_text_never_an_exception(
        self, monkeypatch, wallet
    ):
        """An MCP tool that raises just looks broken to the calling agent."""
        from mcp.types import TextContent

        monkeypatch.setattr(settings, "require_verified_skills", True)
        out = await self._run(monkeypatch, _skill("unverified"), wallet, AsyncMock())
        assert isinstance(out[0], TextContent)


class TestMcpBrokerPath:
    """Cross-node buys are refused under the policy — fail closed.

    A cached remote listing carries no verification status this node can trust:
    CachedSkill has no such column, and merge_discovery neutralizes peer badges to
    'unverified' on ingest precisely because a peer asserting its own provider is
    verified is just a peer talking about itself. So under the policy a peer-hosted
    skill can never satisfy it, and the honest answer is a refusal that says why.
    """

    @pytest.fixture(autouse=True)
    def _remote_skill(self, monkeypatch):
        import conduit.mcp_server as mcp

        # mcp_server imports settings INSIDE the handler, so patch the singleton.
        monkeypatch.setattr(settings, "federation_enabled", True)
        monkeypatch.setattr(settings, "federation_execution_enabled", True)

        async def cached(_session, _skill_id):
            return True

        async def not_local(_session, _skill_id):
            return None

        monkeypatch.setattr(mcp, "is_cached_skill", cached)
        monkeypatch.setattr(mcp, "_find_skill_by_id", not_local)
        monkeypatch.setattr(mcp, "async_session_factory", _session_factory(AsyncMock()))

    async def test_peer_hosted_skill_is_refused_before_the_peer_is_contacted(
        self, monkeypatch
    ):
        import conduit.mcp_server as mcp

        monkeypatch.setattr(settings, "require_verified_skills", True)
        reached = []

        async def boom(*a, **kw):
            reached.append(a)
            raise AssertionError("peer must not be contacted under the policy")

        monkeypatch.setattr(mcp, "resolve_peer_url", boom)
        monkeypatch.setattr(mcp, "get_cached_listing", boom)
        monkeypatch.setattr(mcp, "request_remote_execution", boom)

        out = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})

        assert "refused" in out[0].text.lower()
        assert reached == []

    async def test_brokering_still_works_with_the_policy_off(self, monkeypatch):
        import conduit.mcp_server as mcp
        from conduit.models.cached_skill import CachedSkill

        monkeypatch.setattr(settings, "require_verified_skills", False)

        async def fake_resolve(_session, _skill_id):
            return "https://peer-a.example"

        async def fake_listing(_session, skill_id):
            return CachedSkill(
                provider_pubkey="ab" * 32,
                skill_id=skill_id,
                event_id="cd" * 32,
                event_created_at=1,
                origin="peer",
                source_id="https://peer-a.example",
                name="Remote Indexer",
                price_sats=120,
            )

        async def fake_request(_peer_url, **kwargs):
            return {
                "execution_id": str(uuid.uuid4()),
                "skill_name": "Remote Indexer",
                "price_sats": 120,
                "platform_fee_sats": 2,
                "provider_receives_sats": 118,
                "total_cost_sats": 120,
                "payment_hash": "ab" * 32,
                "payment_request": "lnbc1180n1pfake",
            }

        monkeypatch.setattr(mcp, "resolve_peer_url", fake_resolve)
        monkeypatch.setattr(mcp, "get_cached_listing", fake_listing)
        monkeypatch.setattr(mcp, "request_remote_execution", fake_request)

        out = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})
        assert "peer-a.example" in out[0].text

    async def test_the_rest_broker_fails_closed_the_same_way(self, monkeypatch):
        """The middleware sits on this path but cannot help: it resolves the id as
        a LOCAL Skill row, finds none for a peer-hosted skill, and passes through.
        Both front doors have to refuse, or the policy is one HTTP call away."""
        from fastapi import HTTPException

        from conduit.api.routers import marketplace as mkt

        monkeypatch.setattr(settings, "require_verified_skills", True)

        async def boom(*a, **kw):
            raise AssertionError("peer must not be contacted under the policy")

        monkeypatch.setattr(mkt, "resolve_peer_url", boom)
        monkeypatch.setattr(mkt, "get_cached_listing", boom)
        monkeypatch.setattr(mkt, "request_remote_execution", boom)

        req = mkt.RequestExecutionRequest(
            skill_id=str(uuid.uuid4()), consumer_name="agent"
        )
        with pytest.raises(HTTPException) as exc:
            await mkt._broker_remote_execution(req, AsyncMock())
        assert exc.value.status_code == 403
        assert exc.value.detail["error"] == "skill_not_verified"


class TestConfirmIsDeliberatelyNotGated:
    """Confirm must never be gated on the policy — the buyer has already paid.

    The REST middleware's regex named the confirm path but its exact-path check
    never used it, so confirm was ungated by accident. That is the right outcome
    for the wrong reason: blocking here would pocket a settled payment and withhold
    the result, with no refund path. Making it deliberate (and testing it) is what
    stops someone from 'fixing' the inconsistency in the dangerous direction.
    """

    PREIMAGE = "ab" * 32

    @property
    def _hash(self):
        return hashlib.sha256(bytes.fromhex(self.PREIMAGE)).hexdigest()

    def _execution(self, skill_id):
        from conduit.models.execution import ExecutionStatus

        row = MagicMock()
        row.id = uuid.uuid4()
        row.skill_id = skill_id
        row.status = ExecutionStatus.PENDING_PAYMENT
        row.payment_hash = self._hash
        row.fee_payment_hash = None
        row.platform_fee_sats = 0
        row.fee_invoice_source = None
        row.amount_sats = 1000
        row.payer_pubkey = None
        row.consumer_name = "c"
        row.input_data = {}
        return row

    async def test_mcp_confirm_delivers_even_though_the_skill_is_unverified(
        self, monkeypatch
    ):
        import conduit.mcp_server as mcp

        monkeypatch.setattr(settings, "require_verified_skills", True)
        skill = _skill("unverified", endpoint_url=None)
        session = _session_seq([self._execution(skill.id), skill])

        wallet = MagicMock()
        wallet.lookup_invoice.return_value = {"settled": True}

        monkeypatch.setattr(mcp, "async_session_factory", _session_factory(session))
        monkeypatch.setattr(mcp, "get_lnd", lambda: wallet)
        monkeypatch.setattr(mcp, "check_for_anomalies", AsyncMock(return_value=[]))

        out = await mcp._confirm_skill_execution(
            {"execution_id": str(uuid.uuid4()), "payment_preimage": self.PREIMAGE}
        )

        text = out[0].text
        assert "Payment Confirmed" in text
        assert "refused" not in text.lower()

    # The REST half of this decision lives in
    # tests/test_api.py::TestVerificationMiddleware (it needs the `api` fixture).
