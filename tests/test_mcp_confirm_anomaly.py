"""MCP confirm must not fail over anomaly detection (parity with the REST twin).

The buyer has already paid by the time check_for_anomalies runs inside
_confirm_skill_execution, so a detector hiccup (e.g. a DB error) must never
crash the confirm — the REST confirm in api/routers/marketplace.py already
swallows it with "Don't fail confirm over anomaly detection".

Drives _confirm_skill_execution directly with the DB session, wallet, and
detector stubbed at the boundary (the tests/test_confirm_flow.py pattern).
No Lightning, no Postgres, no network.
"""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from conduit.mcp_server import _confirm_skill_execution
from conduit.models.execution import ExecutionStatus

PREIMAGE = "ab" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()


class _SessionCM:
    """Async context manager yielding a stubbed session (async_session_factory())."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


def _make_execution(skill_id):
    ex = MagicMock()
    ex.id = uuid.uuid4()
    ex.status = ExecutionStatus.PENDING_PAYMENT
    ex.payment_hash = PAYMENT_HASH
    ex.payment_preimage = None
    ex.fee_payment_hash = None  # no fee invoice -> fee check is skipped
    ex.platform_fee_sats = 0
    ex.fee_settled = False
    ex.skill_id = skill_id
    ex.input_data = {"text": "hello world"}
    ex.consumer_name = "consumer"
    ex.amount_sats = 1000
    ex.payer_pubkey = None
    return ex


def _make_skill(skill_id, endpoint=None):
    sk = MagicMock()
    sk.id = skill_id
    sk.name = "demo"
    sk.provider_name = "provider"
    sk.endpoint_url = endpoint
    sk.total_executions = 0
    return sk


def _result(obj):
    r = MagicMock()
    r.scalar_one_or_none.return_value = obj
    return r


async def _run_confirm(execution, skill, anomaly_mock):
    session = AsyncMock()
    session.execute.side_effect = [_result(execution), _result(skill)]
    wallet = MagicMock()
    wallet.lookup_invoice.return_value = {"settled": True, "state": "SETTLED"}

    with (
        patch(
            "conduit.mcp_server.async_session_factory",
            return_value=_SessionCM(session),
        ),
        patch("conduit.mcp_server.get_lnd", return_value=wallet),
        patch("conduit.mcp_server.mint_execution_binding", return_value=None),
        patch("conduit.mcp_server.check_for_anomalies", new=anomaly_mock),
    ):
        result = await _confirm_skill_execution(
            {"execution_id": str(execution.id), "payment_preimage": PREIMAGE}
        )
    return result, session


async def test_anomaly_detector_failure_does_not_fail_confirm():
    """A detector crash after payment settles must not crash the confirm."""
    skill_id = uuid.uuid4()
    execution = _make_execution(skill_id)
    skill = _make_skill(skill_id)
    detector = AsyncMock(side_effect=RuntimeError("anomaly DB hiccup"))

    result, session = await _run_confirm(execution, skill, detector)

    detector.assert_awaited_once()
    assert "Payment Confirmed!" in result[0].text
    assert execution.status == ExecutionStatus.COMPLETED
    session.commit.assert_awaited()


async def test_anomaly_flags_still_reported_when_detector_works():
    """The swallow is for failures only — real flags still reach the response."""
    skill_id = uuid.uuid4()
    execution = _make_execution(skill_id)
    skill = _make_skill(skill_id)
    flag = MagicMock(severity="high", flag_type="velocity", description="too fast")
    detector = AsyncMock(return_value=[flag])

    result, _session = await _run_confirm(execution, skill, detector)

    assert "Payment Confirmed!" in result[0].text
    assert "Anomaly Detection: 1 flag(s) raised" in result[0].text
    assert "[HIGH] velocity: too fast" in result[0].text
    assert execution.status == ExecutionStatus.COMPLETED
