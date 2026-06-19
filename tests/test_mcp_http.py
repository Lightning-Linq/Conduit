"""The HTTP MCP transport must be API-key gated (it controls a Lightning wallet)."""

from starlette.testclient import TestClient

from conduit.mcp_http import build_app

# Matches CONDUIT_API_KEY set by conftest.
AUTH = {"X-API-Key": "test-api-key-for-unit-tests"}
_PING = {"jsonrpc": "2.0", "method": "ping", "id": 1}


def test_unauthenticated_request_is_rejected():
    with TestClient(build_app()) as client:
        r = client.post("/mcp", json=_PING)
        assert r.status_code == 401


def test_wrong_key_is_rejected():
    with TestClient(build_app()) as client:
        r = client.post("/mcp", headers={"X-API-Key": "nope"}, json=_PING)
        assert r.status_code == 401


def test_valid_key_passes_the_auth_gate():
    # With the right key the request clears auth and reaches the MCP layer (which may
    # then 4xx for a missing session/Accept header) — the point is it is NOT a 401.
    with TestClient(build_app()) as client:
        r = client.post("/mcp", headers=AUTH, json=_PING)
        assert r.status_code != 401
