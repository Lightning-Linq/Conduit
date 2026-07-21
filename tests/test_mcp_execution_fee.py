"""MCP execution handler — fee-inclusive split (seller-pays model).

Mirrors the REST-side tests in test_api.py: the provider invoice is for
price - fee, the fee invoice covers the rest, and the buyer's total equals
the listed price. Session factory + wallet are mocked at the module seam,
matching the conftest no-DB convention.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import conduit.mcp_server as mcp


def _session_factory(session: AsyncMock) -> MagicMock:
    """A factory whose `async with factory() as s:` yields `session`."""
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    return MagicMock(return_value=ctx)


def _skill(price_sats: int) -> MagicMock:
    skill = MagicMock()
    skill.id = uuid.uuid4()
    skill.name = "Test Skill"
    skill.provider_name = "acme"
    skill.price_sats = price_sats
    return skill


@pytest.mark.asyncio
async def test_mcp_execution_fee_inclusive_split():
    session = AsyncMock()
    wallet = MagicMock()
    wallet.create_invoice.side_effect = [
        MagicMock(payment_request="lnbc-fee", payment_hash="d" * 64),
        MagicMock(payment_request="lnbc-provider", payment_hash="a" * 64),
    ]

    with patch.object(mcp, "async_session_factory", _session_factory(session)), \
         patch.object(mcp, "get_lnd", return_value=wallet), \
         patch.object(mcp, "_find_skill_by_id", AsyncMock(return_value=_skill(1000))):
        result = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})

    text = result[0].text
    assert "Skill Execution Requested" in text
    assert "Provider receives: 985 sats" in text
    assert "Total cost: 1000 sats" in text

    # Fee invoice first (platform routing decides its wallet), then provider net.
    amounts = [c.kwargs["amount_msats"] for c in wallet.create_invoice.call_args_list]
    assert amounts == [15_000, 985_000]


@pytest.mark.asyncio
async def test_mcp_execution_fee_via_platform_wallet():
    """Configured platform wallet issues the fee invoice (MCP mirror)."""
    session = AsyncMock()
    wallet = MagicMock()
    wallet.create_invoice.return_value = MagicMock(
        payment_request="lnbc-provider", payment_hash="a" * 64
    )
    platform = MagicMock()
    platform.create_invoice.return_value = MagicMock(
        payment_request="lnbc-platform-fee", payment_hash="e" * 64
    )

    with patch.object(mcp, "async_session_factory", _session_factory(session)), \
         patch.object(mcp, "get_lnd", return_value=wallet), \
         patch.object(mcp, "get_platform_wallet", return_value=platform), \
         patch.object(mcp, "_find_skill_by_id", AsyncMock(return_value=_skill(1000))):
        result = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})

    assert "Provider receives: 985 sats" in result[0].text
    assert platform.create_invoice.call_args.kwargs["amount_msats"] == 15_000
    assert wallet.create_invoice.call_count == 1
    assert wallet.create_invoice.call_args.kwargs["amount_msats"] == 985_000

    execution = session.add.call_args[0][0]
    assert execution.fee_invoice_source == "platform"


@pytest.mark.asyncio
async def test_mcp_execution_platform_wallet_failure_fails_open():
    """Platform wallet down: sale proceeds, provider gets the full price."""
    session = AsyncMock()
    wallet = MagicMock()
    wallet.create_invoice.return_value = MagicMock(
        payment_request="lnbc-provider", payment_hash="a" * 64
    )
    platform = MagicMock()
    platform.create_invoice.side_effect = RuntimeError("relay timeout")

    with patch.object(mcp, "async_session_factory", _session_factory(session)), \
         patch.object(mcp, "get_lnd", return_value=wallet), \
         patch.object(mcp, "get_platform_wallet", return_value=platform), \
         patch.object(mcp, "_find_skill_by_id", AsyncMock(return_value=_skill(1000))):
        result = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})

    text = result[0].text
    assert "Platform fee: 0 sats" in text
    assert "Provider receives: 1000 sats" in text
    assert wallet.create_invoice.call_count == 1
    assert wallet.create_invoice.call_args.kwargs["amount_msats"] == 1_000_000

    execution = session.add.call_args[0][0]
    assert execution.fee_invoice_source is None
    assert execution.platform_fee_sats == 0


def _confirmable_execution(fee_source):
    """Execution row ready to confirm; doubles as the skill row (the mocked
    session returns the same object for both lookups)."""
    import hashlib

    from conduit.models.execution import ExecutionStatus

    preimage = "ab" * 32
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    row = MagicMock()
    row.status = ExecutionStatus.PENDING_PAYMENT
    row.payment_hash = payment_hash
    row.fee_payment_hash = "f" * 64
    row.platform_fee_sats = 15
    row.fee_invoice_source = fee_source
    row.fee_settled = False
    row.amount_sats = 1000
    row.payer_pubkey = None
    row.consumer_name = "c"
    row.name = "Test Skill"
    row.provider_name = "acme"
    row.endpoint_url = None
    return row, preimage


def _confirm_session(row) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    session.execute = AsyncMock(return_value=result)
    return session


@pytest.mark.asyncio
async def test_mcp_confirm_verifies_fee_on_platform_wallet():
    row, preimage = _confirmable_execution("platform")
    session = _confirm_session(row)
    wallet = MagicMock()
    wallet.lookup_invoice.return_value = {"settled": True}
    platform = MagicMock()
    platform.lookup_invoice.return_value = {"settled": True}

    with patch.object(mcp, "async_session_factory", _session_factory(session)), \
         patch.object(mcp, "get_lnd", return_value=wallet), \
         patch.object(mcp, "get_platform_wallet", return_value=platform), \
         patch.object(mcp, "check_for_anomalies", AsyncMock(return_value=[])):
        await mcp._confirm_skill_execution(
            {"execution_id": str(uuid.uuid4()), "payment_preimage": preimage}
        )

    platform.lookup_invoice.assert_called_once_with("f" * 64)
    wallet.lookup_invoice.assert_called_once_with(row.payment_hash)
    assert row.fee_settled is True


@pytest.mark.asyncio
async def test_mcp_confirm_platform_fee_unverifiable_aborts():
    """Platform-issued fee invoice, no platform wallet: strict abort."""
    row, preimage = _confirmable_execution("platform")
    session = _confirm_session(row)
    wallet = MagicMock()
    wallet.lookup_invoice.return_value = {"settled": True}

    with patch.object(mcp, "async_session_factory", _session_factory(session)), \
         patch.object(mcp, "get_lnd", return_value=wallet), \
         patch.object(mcp, "get_platform_wallet", return_value=None):
        result = await mcp._confirm_skill_execution(
            {"execution_id": str(uuid.uuid4()), "payment_preimage": preimage}
        )

    assert "could not be verified" in result[0].text.lower()
    assert row.fee_settled is False


@pytest.mark.asyncio
async def test_mcp_execution_inactive_skill_refused():
    """Executing a deactivated listing is refused before any invoice (MCP)."""
    session = AsyncMock()
    wallet = MagicMock()
    skill = _skill(1000)
    skill.is_active = False

    with patch.object(mcp, "async_session_factory", _session_factory(session)), \
         patch.object(mcp, "get_lnd", return_value=wallet), \
         patch.object(mcp, "_find_skill_by_id", AsyncMock(return_value=skill)):
        result = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})

    assert "inactive" in result[0].text.lower()
    wallet.create_invoice.assert_not_called()
    session.add.assert_not_called()


@pytest.mark.asyncio
async def test_mcp_execution_below_waive_floor_single_invoice():
    session = AsyncMock()
    wallet = MagicMock()
    wallet.create_invoice.return_value = MagicMock(
        payment_request="lnbc-provider", payment_hash="a" * 64
    )

    with patch.object(mcp, "async_session_factory", _session_factory(session)), \
         patch.object(mcp, "get_lnd", return_value=wallet), \
         patch.object(mcp, "_find_skill_by_id", AsyncMock(return_value=_skill(5))):
        result = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})

    text = result[0].text
    assert "Platform fee: 0 sats" in text
    assert "Pay this invoice to proceed" in text
    assert wallet.create_invoice.call_count == 1
    assert wallet.create_invoice.call_args.kwargs["amount_msats"] == 5_000
