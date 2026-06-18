"""Layer C: the confirm -> webhook -> deliver orchestration, over HTTP.

Drives POST /executions/{id}/confirm through the real FastAPI app with the
wallet and DB stubbed at the boundary (the tests/test_api.py pattern): the
wallet reports the invoice settled, the DB returns a pending execution and its
skill, and the webhook executor is mocked. The point is the orchestration, a
settled payment must trigger the provider webhook with the correct contract and
return its output; the executor itself is covered by tests/test_skill_executor.

No Lightning, no Postgres, no network.
"""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from conduit.api.deps import get_session
from conduit.main import app
from conduit.models.execution import ExecutionStatus
from conduit.services.rate_limiter import rate_limiter
from conduit.services.skill_executor import SkillExecutionError

AUTH = {"X-API-Key": "test-api-key-for-unit-tests"}

PREIMAGE = "ab" * 32
PAYMENT_HASH = hashlib.sha256(bytes.fromhex(PREIMAGE)).hexdigest()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limiter._redis_available = False
    rate_limiter._memory._windows.clear()
    yield
    rate_limiter._memory._windows.clear()


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


def _make_skill(skill_id, endpoint="https://provider.test/skills/demo"):
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


def _scalar_result(value):
    """A query result whose .scalar() returns `value` (e.g. a COUNT)."""
    r = MagicMock()
    r.scalar.return_value = value
    return r


@pytest.fixture
def confirm_ctx():
    """Yield (client, session, wallet, webhook) with the boundary stubbed."""
    session = AsyncMock()
    session.commit = AsyncMock()
    wallet = MagicMock()
    wallet.lookup_invoice.return_value = {"settled": True, "state": "SETTLED"}

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    with (
        patch("conduit.api.routers.marketplace.get_lnd", return_value=wallet),
        patch(
            "conduit.api.routers.marketplace.execute_skill_webhook", new=AsyncMock()
        ) as webhook,
        patch(
            "conduit.api.routers.marketplace.mint_execution_binding", return_value=None
        ),
        patch(
            "conduit.api.routers.marketplace.get_node_keypair",
            return_value=MagicMock(pubkey_hex="02ab"),
        ),
        patch(
            "conduit.api.routers.marketplace.check_for_anomalies",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "conduit.api.middleware.verification."
            "VerificationEnforcementMiddleware._get_verification_status",
            new=AsyncMock(return_value=None),
        ),
    ):
        yield TestClient(app), session, wallet, webhook
    app.dependency_overrides.pop(get_session, None)


def _confirm(client, exec_id):
    return client.post(
        f"/api/v1/marketplace/executions/{exec_id}/confirm",
        json={"payment_hash": PAYMENT_HASH, "payment_preimage": PREIMAGE},
        headers=AUTH,
    )


def test_settled_payment_fires_webhook_and_returns_output(confirm_ctx):
    client, session, wallet, webhook = confirm_ctx
    skill_id = uuid.uuid4()
    execution = _make_execution(skill_id)
    skill = _make_skill(skill_id)
    # Third execute is REQ-02's first-time-provider lookup in build_rating_prompt;
    # 0 prior completed executions -> the consumer is prompted to rate.
    session.execute.side_effect = [
        _result(execution),
        _result(skill),
        _scalar_result(0),
    ]
    webhook.return_value = {"output": {"hex": "b94d27b9"}, "execution_time_ms": 7}

    resp = _confirm(client, execution.id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["output"] == {"hex": "b94d27b9"}
    assert body["execution_time_ms"] == 7
    assert body["should_prompt_rating"] is True
    assert body["rating_policy"] == "first_time_provider"

    webhook.assert_awaited_once()
    kwargs = webhook.await_args.kwargs
    assert kwargs["endpoint_url"] == "https://provider.test/skills/demo"
    assert kwargs["input_data"] == {"text": "hello world"}
    assert kwargs["payment_hash"] == PAYMENT_HASH
    assert kwargs["payment_preimage"] == PREIMAGE
    assert kwargs["skill_name"] == "demo"


def test_unsettled_payment_returns_402_and_skips_webhook(confirm_ctx):
    client, session, wallet, webhook = confirm_ctx
    wallet.lookup_invoice.return_value = {"settled": False}
    execution = _make_execution(uuid.uuid4())
    session.execute.side_effect = [_result(execution)]

    resp = _confirm(client, execution.id)

    assert resp.status_code == 402
    webhook.assert_not_awaited()


def test_webhook_failure_returns_502(confirm_ctx):
    client, session, wallet, webhook = confirm_ctx
    skill_id = uuid.uuid4()
    execution = _make_execution(skill_id)
    skill = _make_skill(skill_id)
    session.execute.side_effect = [_result(execution), _result(skill)]
    webhook.side_effect = SkillExecutionError("provider exploded")

    resp = _confirm(client, execution.id)

    assert resp.status_code == 502
    assert "skill_execution_failed" in resp.text


def test_skill_without_endpoint_completes_with_payment_proof(confirm_ctx):
    client, session, wallet, webhook = confirm_ctx
    skill_id = uuid.uuid4()
    execution = _make_execution(skill_id)
    skill = _make_skill(skill_id, endpoint=None)
    session.execute.side_effect = [_result(execution), _result(skill)]

    resp = _confirm(client, execution.id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["output"]["payment_proof"]["payment_preimage"] == PREIMAGE
    webhook.assert_not_awaited()


def test_executing_status_is_resumable_and_reruns_webhook(confirm_ctx):
    """N5: a row stranded in EXECUTING (crash/DB failure mid-delivery) re-delivers
    on the next confirm instead of wedging at 409."""
    client, session, wallet, webhook = confirm_ctx
    skill_id = uuid.uuid4()
    execution = _make_execution(skill_id)
    execution.status = ExecutionStatus.EXECUTING
    skill = _make_skill(skill_id)
    session.execute.side_effect = [
        _result(execution),
        _result(skill),
        _scalar_result(0),
    ]
    webhook.return_value = {"output": {"hex": "deadbeef"}, "execution_time_ms": 3}

    resp = _confirm(client, execution.id)

    assert resp.status_code == 200, resp.text
    assert resp.json()["output"] == {"hex": "deadbeef"}
    webhook.assert_awaited_once()  # re-delivered rather than 409


def test_completed_status_still_conflicts(confirm_ctx):
    """A terminal execution is not resumable: confirm returns 409, no webhook."""
    client, session, wallet, webhook = confirm_ctx
    execution = _make_execution(uuid.uuid4())
    execution.status = ExecutionStatus.COMPLETED
    session.execute.side_effect = [_result(execution)]

    resp = _confirm(client, execution.id)

    assert resp.status_code == 409
    webhook.assert_not_awaited()
