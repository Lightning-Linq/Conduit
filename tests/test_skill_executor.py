"""Tests for the webhook skill executor — Conduit's "deliver the skill" leg.

These cover the path that fires after payment is verified: build the contract
payload, POST it to the provider's endpoint, and normalize the response. The
HTTP layer is mocked with httpx.MockTransport so the suite stays offline; the
SSRF guard is exercised against real resolution (scheme + literal-IP checks
need no network). An opt-in live test hits the deployed provider.
"""

import hashlib
import json
import os

import httpx
import pytest

from conduit.services import skill_executor
from conduit.services.skill_executor import SkillExecutionError, execute_skill_webhook

DEFAULTS = dict(
    endpoint_url="https://provider.test/skills/demo",
    input_data={"text": "hi"},
    payment_hash="aa" * 32,
    payment_preimage="bb" * 32,
    skill_name="demo",
    execution_id="exec-123",
)


def _bypass_resolution(monkeypatch):
    """Skip DNS/SSRF resolution so HTTP-layer tests stay fully offline."""
    monkeypatch.setattr(
        skill_executor,
        "resolve_and_validate",
        lambda url: (url, "provider.test", ["93.184.216.34"]),
    )


def _mock_http(monkeypatch, handler):
    """Route the executor's AsyncClient through an httpx MockTransport."""
    real_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(skill_executor.httpx, "AsyncClient", factory)


async def _run(**overrides):
    return await execute_skill_webhook(**{**DEFAULTS, **overrides})


async def test_sends_full_contract_payload(monkeypatch):
    captured = {}

    def handler(request):
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["headers"] = request.headers
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"output": {"ok": True}})

    _bypass_resolution(monkeypatch)
    _mock_http(monkeypatch, handler)

    res = await _run()

    body = captured["body"]
    assert captured["method"] == "POST"
    assert body["execution_id"] == "exec-123"
    assert body["skill_name"] == "demo"
    assert body["input_data"] == {"text": "hi"}
    assert body["payment_proof"] == {
        "payment_hash": "aa" * 32,
        "payment_preimage": "bb" * 32,
    }
    assert "timestamp" in body
    assert captured["headers"]["x-conduit-execution-id"] == "exec-123"
    assert res["output"] == {"ok": True}
    assert "execution_time_ms" in res


async def test_wraps_flat_response_as_output(monkeypatch):
    def handler(request):
        return httpx.Response(200, json={"algorithm": "sha256", "hex": "abc"})

    _bypass_resolution(monkeypatch)
    _mock_http(monkeypatch, handler)

    res = await _run()
    assert res["output"] == {"algorithm": "sha256", "hex": "abc"}


async def test_provider_client_error_raises(monkeypatch):
    def handler(request):
        return httpx.Response(400, text="bad input")

    _bypass_resolution(monkeypatch)
    _mock_http(monkeypatch, handler)

    with pytest.raises(SkillExecutionError) as ei:
        await _run()
    assert "400" in str(ei.value)


async def test_provider_server_error_raises(monkeypatch):
    def handler(request):
        return httpx.Response(503, text="upstream down")

    _bypass_resolution(monkeypatch)
    _mock_http(monkeypatch, handler)

    with pytest.raises(SkillExecutionError) as ei:
        await _run()
    assert "503" in str(ei.value)


async def test_redirect_not_followed_raises(monkeypatch):
    def handler(request):
        return httpx.Response(302, headers={"Location": "https://evil.test/"})

    _bypass_resolution(monkeypatch)
    _mock_http(monkeypatch, handler)

    with pytest.raises(SkillExecutionError) as ei:
        await _run()
    assert "redirect" in str(ei.value).lower()


async def test_invalid_json_raises(monkeypatch):
    def handler(request):
        return httpx.Response(200, text="this is not json")

    _bypass_resolution(monkeypatch)
    _mock_http(monkeypatch, handler)

    with pytest.raises(SkillExecutionError) as ei:
        await _run()
    assert "json" in str(ei.value).lower()


async def test_timeout_raises(monkeypatch):
    def handler(request):
        raise httpx.TimeoutException("slow")

    _bypass_resolution(monkeypatch)
    _mock_http(monkeypatch, handler)

    with pytest.raises(SkillExecutionError) as ei:
        await _run()
    assert "timed out" in str(ei.value).lower()


async def test_connect_error_raises(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("no route")

    _bypass_resolution(monkeypatch)
    _mock_http(monkeypatch, handler)

    with pytest.raises(SkillExecutionError) as ei:
        await _run()
    assert "connect" in str(ei.value).lower()


async def test_strips_control_chars_from_error_excerpt(monkeypatch):
    def handler(request):
        return httpx.Response(400, text="bad\x1b[31m\x00secret")

    _bypass_resolution(monkeypatch)
    _mock_http(monkeypatch, handler)

    with pytest.raises(SkillExecutionError) as ei:
        await _run()
    msg = str(ei.value)
    assert "\x1b" not in msg and "\x00" not in msg


async def test_rejects_non_https_scheme():
    with pytest.raises(SkillExecutionError) as ei:
        await _run(endpoint_url="http://example.com/skills/demo")
    assert "Refusing to call" in str(ei.value)


async def test_rejects_loopback_address():
    with pytest.raises(SkillExecutionError) as ei:
        await _run(endpoint_url="https://127.0.0.1/skills/demo")
    assert "Refusing to call" in str(ei.value)


@pytest.mark.skipif(
    not os.getenv("CONDUIT_LIVE_TESTS"),
    reason="set CONDUIT_LIVE_TESTS=1 to hit the live deployed provider",
)
async def test_live_provider_hash_digest():
    preimage = os.urandom(32).hex()
    payment_hash = hashlib.sha256(bytes.fromhex(preimage)).hexdigest()
    res = await execute_skill_webhook(
        endpoint_url="https://skills.lightninglinq.ai/skills/hash-digest",
        input_data={"text": "hello world"},
        payment_hash=payment_hash,
        payment_preimage=preimage,
        skill_name="hash-digest",
        execution_id="live-test",
    )
    assert (
        res["output"]["hex"]
        == "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )
