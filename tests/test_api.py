"""HTTP-layer integration tests for the Conduit REST API.

These exercise the real FastAPI app (`conduit.main:app`) end to end through
Starlette's TestClient: routing, the middleware stack (rate-limit, L402,
verification), API-key auth, request validation, and response serialization.

What is stubbed vs. real:
  - REAL: the app, all middleware, routers, dependency wiring, pydantic
    validation, and status-code mapping.
  - STUBBED: the wallet backend (`get_lnd`), the DB session (`get_session`),
    and a handful of services patched at the router boundary. A real DB is
    out of scope here — the models use Postgres-only types (JSONB/UUID) and
    the underlying services are covered by their own unit tests. The goal is
    to lock down the HTTP surface, which previously had zero coverage.

The TestClient is intentionally NOT used as a context manager, so the
LND-connecting `lifespan` (which also does a fatal file-permission check)
never runs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.testclient import TestClient

from conduit.api.deps import get_session
from conduit.main import app
from conduit.services.rate_limiter import rate_limiter

# Matches the key set by conftest.py.
API_KEY = "test-api-key-for-unit-tests"
AUTH = {"X-API-Key": API_KEY}

# A syntactically valid UUID for path params (no DB row need exist).
SKILL_UUID = "11111111-1111-1111-1111-111111111111"
EXEC_UUID = "22222222-2222-2222-2222-222222222222"


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Force the limiter to deterministic in-memory mode and clear state.

    The middleware records a call for every matched route (even ones that
    will 401/422), so without a per-test reset, low-limit endpoints would
    flake. Function-scoped + autouse → every test (and every parametrize
    case) starts with an empty window.
    """
    rate_limiter._redis_available = False
    rate_limiter._memory._windows.clear()
    yield
    rate_limiter._memory._windows.clear()


def _make_wallet() -> MagicMock:
    """A fake WalletBackend with sane return values for every method used."""
    w = MagicMock()
    w.get_info.return_value = MagicMock(
        alias="test-node",
        pubkey="02" + "ab" * 32,
        num_active_channels=3,
        num_peers=5,
        block_height=800_000,
        synced_to_chain=True,
        version="0.17.0-beta",
    )
    w.get_balance.return_value = {
        "channel_balance_sats": 50_000,
        "onchain_balance_sats": 100_000,
    }
    w.create_invoice.return_value = MagicMock(
        payment_request="lnbc1000n1fake",
        payment_hash="a" * 64,
    )
    w.decode_invoice.return_value = {
        "amount_sats": 1_000,
        "description": "test invoice",
        "payment_hash": "b" * 64,
    }
    w.pay_invoice.return_value = MagicMock(
        status="SUCCEEDED",
        payment_hash="b" * 64,
        preimage="c" * 64,
        fee_msats=1_000,
        failure_reason=None,
    )
    w.lookup_invoice.return_value = {
        "settled": True,
        "state": "SETTLED",
        "amount_sats": 1_000,
    }
    return w


def _make_session() -> AsyncMock:
    """An AsyncMock DB session with empty/zero defaults.

    Defaults let read endpoints return empty results and lookups 404
    without per-test setup. Tests that need a row override
    `session.execute.return_value`.
    """
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    result.scalar.return_value = 0
    result.scalar_one_or_none.return_value = None
    result.all.return_value = []
    session.execute = AsyncMock(return_value=result)
    session.commit = AsyncMock()
    session.delete = AsyncMock()
    session.get = AsyncMock(return_value=None)
    return session


class ApiCtx:
    """Bundle handed to each test: the client plus the stubs behind it."""

    def __init__(self, client: TestClient, session: AsyncMock, wallet: MagicMock):
        self.client = client
        self.session = session
        self.wallet = wallet


@pytest.fixture
def api():
    """Install dependency overrides + wallet patches, yield an ApiCtx."""
    session = _make_session()
    wallet = _make_wallet()

    async def _session_override():
        yield session

    app.dependency_overrides[get_session] = _session_override
    with patch("conduit.api.routers.lightning.get_lnd", return_value=wallet), patch(
        "conduit.api.routers.marketplace.get_lnd", return_value=wallet
    ), patch("conduit.api.routers.security.get_lnd", return_value=wallet), patch(
        # The verification middleware uses the globally-mocked session factory,
        # not our override. Default it to "unknown" (None) so it passes through;
        # TestVerificationMiddleware re-patches this to exercise warn/block.
        "conduit.api.middleware.verification."
        "VerificationEnforcementMiddleware._get_verification_status",
        AsyncMock(return_value=None),
    ):
        yield ApiCtx(TestClient(app), session, wallet)
    app.dependency_overrides.pop(get_session, None)


# Every protected endpoint: (method, path, json_body|None).
# Bodies/query params are valid so the ONLY error in an auth test is the key.
PROTECTED_ENDPOINTS = [
    ("GET", "/api/v1/lightning/node-info", None),
    ("GET", "/api/v1/lightning/balance", None),
    ("POST", "/api/v1/lightning/invoices", {"amount_sats": 100}),
    ("POST", "/api/v1/lightning/invoices/decode", {"payment_request": "lnbc1"}),
    ("POST", "/api/v1/lightning/payments", {"payment_request": "lnbc1"}),
    ("GET", f"/api/v1/lightning/payments/{'b' * 64}", None),
    ("GET", "/api/v1/marketplace/skills", None),
    ("GET", f"/api/v1/marketplace/skills/{SKILL_UUID}", None),
    ("POST", "/api/v1/marketplace/skills",
     {"name": "n", "description": "d", "provider_name": "p"}),
    ("DELETE", f"/api/v1/marketplace/skills/{SKILL_UUID}?provider_name=p", None),
    ("POST", "/api/v1/marketplace/executions", {"skill_id": SKILL_UUID}),
    ("DELETE", f"/api/v1/marketplace/executions/{EXEC_UUID}?consumer_name=c", None),
    ("POST", f"/api/v1/marketplace/executions/{EXEC_UUID}/confirm",
     {"payment_hash": "h", "payment_preimage": "p"}),
    ("POST", f"/api/v1/marketplace/executions/{EXEC_UUID}/rate",
     {"score": 5, "payment_preimage": "p"}),
    ("GET", "/api/v1/security/spending", None),
    ("POST", "/api/v1/security/macaroons", {}),
    ("GET", "/api/v1/security/permissions", None),
    ("GET", "/api/v1/security/anomalies", None),
    ("POST", "/api/v1/security/verification/request",
     {"skill_id": SKILL_UUID, "method": "node"}),
    ("POST", "/api/v1/security/verification/submit",
     {"skill_id": SKILL_UUID, "method": "node"}),
    ("GET", f"/api/v1/security/verification/{SKILL_UUID}", None),
    ("POST", "/api/v1/nostr/publish", {"skill_id": SKILL_UUID}),
    ("GET", "/api/v1/nostr/discover", None),
    ("GET", "/api/v1/nostr/profile", None),
    ("GET", "/api/v1/nostr/relays/status", None),
    ("GET", "/api/v1/admin/stats", None),
    ("DELETE", "/api/v1/admin/reset-demo", None),
]


# ── App-level (unauthenticated) ───────────────────────────────────────


class TestAppRoot:
    def test_health(self, api):
        r = api.client.get("/health")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_root(self, api):
        r = api.client.get("/")
        assert r.status_code == 200
        assert "endpoints" in r.json()

    def test_openapi_schema(self, api):
        r = api.client.get("/openapi.json")
        assert r.status_code == 200
        # The whole REST surface: 28 documented endpoints (+1: federation serve).
        assert len(r.json()["paths"]) == 28

    def test_docs(self, api):
        assert api.client.get("/docs").status_code == 200


# ── Auth enforcement across every protected endpoint ──────────────────


class TestAuth:
    @pytest.mark.parametrize("method,path,body", PROTECTED_ENDPOINTS)
    def test_missing_api_key_rejected(self, api, method, path, body):
        # Missing X-API-Key header → 401 Unauthorized (the correct status for
        # absent credentials), not FastAPI's default 422 for a missing header.
        r = api.client.request(method, path, json=body)
        assert r.status_code == 401

    @pytest.mark.parametrize("method,path,body", PROTECTED_ENDPOINTS)
    def test_wrong_api_key_rejected(self, api, method, path, body):
        r = api.client.request(method, path, json=body, headers={"X-API-Key": "nope"})
        assert r.status_code == 401


# ── Lightning ─────────────────────────────────────────────────────────


class TestLightning:
    def test_node_info(self, api):
        r = api.client.get("/api/v1/lightning/node-info", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["alias"] == "test-node"
        assert body["num_active_channels"] == 3

    def test_balance(self, api):
        r = api.client.get("/api/v1/lightning/balance", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["channel_balance_sats"] == 50_000

    def test_create_invoice(self, api):
        r = api.client.post(
            "/api/v1/lightning/invoices", json={"amount_sats": 500}, headers=AUTH
        )
        assert r.status_code == 200
        assert r.json()["amount_sats"] == 500
        assert r.json()["payment_request"] == "lnbc1000n1fake"
        # amount must be converted to msats for the backend.
        api.wallet.create_invoice.assert_called_once()
        assert api.wallet.create_invoice.call_args.kwargs["amount_msats"] == 500_000

    def test_create_invoice_rejects_nonpositive_amount(self, api):
        r = api.client.post(
            "/api/v1/lightning/invoices", json={"amount_sats": 0}, headers=AUTH
        )
        assert r.status_code == 422  # pydantic gt=0

    def test_create_invoice_missing_amount(self, api):
        r = api.client.post("/api/v1/lightning/invoices", json={}, headers=AUTH)
        assert r.status_code == 422

    def test_decode_invoice(self, api):
        r = api.client.post(
            "/api/v1/lightning/invoices/decode",
            json={"payment_request": "lnbc1"},
            headers=AUTH,
        )
        assert r.status_code == 200
        assert r.json()["amount_sats"] == 1_000

    def test_check_payment_found(self, api):
        r = api.client.get(f"/api/v1/lightning/payments/{'b' * 64}", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["settled"] is True

    def test_check_payment_not_found(self, api):
        api.wallet.lookup_invoice.side_effect = Exception("no such invoice")
        r = api.client.get(f"/api/v1/lightning/payments/{'d' * 64}", headers=AUTH)
        assert r.status_code == 404

    def test_pay_invoice_success(self, api):
        with patch(
            "conduit.api.routers.lightning.check_spending_limits",
            AsyncMock(return_value="reservation-1"),
        ), patch(
            "conduit.api.routers.lightning.record_successful_payment", AsyncMock()
        ), patch(
            "conduit.api.routers.lightning.check_for_anomalies",
            AsyncMock(return_value=[]),
        ):
            r = api.client.post(
                "/api/v1/lightning/payments",
                json={"payment_request": "lnbc1", "max_fee_sats": 5},
                headers=AUTH,
            )
        assert r.status_code == 200
        assert r.json()["status"] == "SUCCEEDED"
        assert r.json()["amount_sats"] == 1_000

    def test_pay_invoice_zero_amount_rejected(self, api):
        api.wallet.decode_invoice.return_value = {
            "amount_sats": 0,
            "description": "",
            "payment_hash": "b" * 64,
        }
        r = api.client.post(
            "/api/v1/lightning/payments",
            json={"payment_request": "lnbc1"},
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_pay_invoice_failed_returns_502(self, api):
        api.wallet.pay_invoice.return_value = MagicMock(
            status="FAILED", payment_hash="b" * 64, failure_reason="no_route", preimage=None
        )
        with patch(
            "conduit.api.routers.lightning.check_spending_limits",
            AsyncMock(return_value="reservation-1"),
        ), patch(
            "conduit.api.routers.lightning.cancel_reservation", AsyncMock()
        ):
            r = api.client.post(
                "/api/v1/lightning/payments",
                json={"payment_request": "lnbc1"},
                headers=AUTH,
            )
        assert r.status_code == 502


# ── Marketplace ───────────────────────────────────────────────────────


class TestMarketplace:
    def test_discover_skills_empty(self, api):
        r = api.client.get("/api/v1/marketplace/skills", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {"count": 0, "skills": []}

    def test_get_skill_details_invalid_uuid(self, api):
        r = api.client.get("/api/v1/marketplace/skills/not-a-uuid", headers=AUTH)
        assert r.status_code == 400

    def test_get_skill_details_not_found(self, api):
        r = api.client.get(f"/api/v1/marketplace/skills/{SKILL_UUID}", headers=AUTH)
        assert r.status_code == 404

    def test_register_skill_validation(self, api):
        # Missing required fields (description, provider_name) → 422.
        r = api.client.post(
            "/api/v1/marketplace/skills", json={"name": "x"}, headers=AUTH
        )
        assert r.status_code == 422

    def test_register_skill_rejects_unsafe_webhook(self, api):
        from conduit.services.url_safety import UnsafeURLError

        with patch(
            "conduit.api.routers.marketplace.validate_outbound_url",
            side_effect=UnsafeURLError("points at localhost"),
        ):
            r = api.client.post(
                "/api/v1/marketplace/skills",
                json={
                    "name": "n",
                    "description": "d",
                    "provider_name": "p",
                    "webhook_url": "http://127.0.0.1/x",
                },
                headers=AUTH,
            )
        assert r.status_code == 400

    def test_request_execution_skill_not_found(self, api):
        r = api.client.post(
            "/api/v1/marketplace/executions", json={"skill_id": SKILL_UUID}, headers=AUTH
        )
        assert r.status_code == 404

    def test_request_execution_rejects_bad_payer_pubkey(self, api):
        r = api.client.post(
            "/api/v1/marketplace/executions",
            json={"skill_id": SKILL_UUID, "payer_pubkey": "nothex"},
            headers=AUTH,
        )
        assert r.status_code == 422  # field_validator rejects non-64-hex

    def test_request_execution_accepts_valid_payer_pubkey(self, api):
        # Valid 64-hex passes validation; the skill is still missing (stubbed
        # session) so we get 404 — proving the field was accepted, not a 422.
        r = api.client.post(
            "/api/v1/marketplace/executions",
            json={"skill_id": SKILL_UUID, "payer_pubkey": "ab" * 32},
            headers=AUTH,
        )
        assert r.status_code == 404

    def test_rate_invalid_score(self, api):
        r = api.client.post(
            f"/api/v1/marketplace/executions/{EXEC_UUID}/rate",
            json={"score": 6, "payment_preimage": "x"},
            headers=AUTH,
        )
        assert r.status_code == 422  # le=5

    def test_rate_invalid_execution_uuid(self, api):
        r = api.client.post(
            "/api/v1/marketplace/executions/not-a-uuid/rate",
            json={"score": 5, "payment_preimage": "x"},
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_delete_skill_not_found(self, api):
        r = api.client.delete(
            f"/api/v1/marketplace/skills/{SKILL_UUID}?provider_name=p", headers=AUTH
        )
        assert r.status_code == 404


# ── Security ──────────────────────────────────────────────────────────


class TestSecurity:
    def test_spending_status(self, api):
        with patch(
            "conduit.api.routers.security.get_spending_summary",
            AsyncMock(return_value={"hourly_spent_sats": 0}),
        ):
            r = api.client.get("/api/v1/security/spending", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["hourly_spent_sats"] == 0

    def test_permissions(self, api):
        with patch(
            "conduit.api.routers.security.get_active_permissions", return_value=None
        ):
            r = api.client.get("/api/v1/security/permissions", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["permissions"] == "unrestricted"

    def test_anomalies(self, api):
        with patch(
            "conduit.api.routers.security.get_anomaly_summary",
            AsyncMock(return_value={"total_flags": 0}),
        ):
            r = api.client.get("/api/v1/security/anomalies", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["total_flags"] == 0

    def test_create_macaroon(self, api):
        with patch(
            "conduit.api.routers.security.derive_macaroon", return_value="macaroon-xyz"
        ):
            r = api.client.post(
                "/api/v1/security/macaroons", json={"profile": "readonly"}, headers=AUTH
            )
        assert r.status_code == 200
        assert r.json()["macaroon"] == "macaroon-xyz"

    def test_create_macaroon_invalid_profile(self, api):
        with patch(
            "conduit.api.routers.security.derive_macaroon",
            side_effect=ValueError("unknown profile"),
        ):
            r = api.client.post(
                "/api/v1/security/macaroons", json={"profile": "bogus"}, headers=AUTH
            )
        assert r.status_code == 400

    def test_verification_request_invalid_method(self, api):
        r = api.client.post(
            "/api/v1/security/verification/request",
            json={"skill_id": SKILL_UUID, "method": "carrier-pigeon"},
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_verification_request_domain_requires_domain(self, api):
        r = api.client.post(
            "/api/v1/security/verification/request",
            json={"skill_id": SKILL_UUID, "method": "domain"},
            headers=AUTH,
        )
        assert r.status_code == 400

    def test_verification_submit_node_requires_signature(self, api):
        r = api.client.post(
            "/api/v1/security/verification/submit",
            json={"skill_id": SKILL_UUID, "method": "node"},
            headers=AUTH,
        )
        assert r.status_code == 400


# ── Nostr ─────────────────────────────────────────────────────────────


class TestNostr:
    def test_discover(self, api):
        with patch(
            "conduit.api.routers.nostr.discover_from_relays",
            AsyncMock(return_value=[]),
        ):
            r = api.client.get("/api/v1/nostr/discover", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["skills"] == []

    def test_profile(self, api):
        r = api.client.get("/api/v1/nostr/profile", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["npub"].startswith("npub")
        assert body["local_skill_count"] == 0

    def test_relay_status(self, api):
        class _FakeRelay:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        with patch("conduit.api.routers.nostr.NostrRelay", _FakeRelay):
            r = api.client.get("/api/v1/nostr/relays/status", headers=AUTH)
        assert r.status_code == 200
        body = r.json()
        assert body["connected_count"] == body["total_count"] == len(body["relays"])
        assert body["total_count"] > 0

    def test_publish_skill_not_found(self, api):
        r = api.client.post(
            "/api/v1/nostr/publish", json={"skill_id": SKILL_UUID}, headers=AUTH
        )
        assert r.status_code == 404

    def test_publish_validation(self, api):
        r = api.client.post("/api/v1/nostr/publish", json={}, headers=AUTH)
        assert r.status_code == 422


# ── Admin ─────────────────────────────────────────────────────────────


class TestAdmin:
    def test_stats(self, api):
        r = api.client.get("/api/v1/admin/stats", headers=AUTH)
        assert r.status_code == 200
        assert r.json() == {
            "skills": 0,
            "executions": 0,
            "ratings": 0,
            "anomaly_flags": 0,
            "total": 0,
        }

    def test_reset_demo(self, api):
        # rowcount comes off the (sync) Result returned by the awaited execute.
        api.session.execute.return_value.rowcount = 0
        r = api.client.delete("/api/v1/admin/reset-demo", headers=AUTH)
        assert r.status_code == 200
        assert r.json()["reset"] is True


# ── Middleware: rate limiting ─────────────────────────────────────────


class TestRateLimitMiddleware:
    def test_create_invoice_429_after_limit(self, api):
        # create_invoice is limited to 15/min; the 16th call is rejected.
        path = "/api/v1/lightning/invoices"
        body = {"amount_sats": 100}
        for i in range(15):
            r = api.client.post(path, json=body, headers=AUTH)
            assert r.status_code == 200, f"call {i + 1} should pass"
        r = api.client.post(path, json=body, headers=AUTH)
        assert r.status_code == 429
        assert "Retry-After" in r.headers
        assert r.json()["error"] == "rate_limit_exceeded"


# ── Middleware: provider verification enforcement ─────────────────────


_VERIFY_TARGET = (
    "conduit.api.middleware.verification."
    "VerificationEnforcementMiddleware._get_verification_status"
)


class TestVerificationMiddleware:
    def test_unverified_skill_adds_warning_header(self, api):
        # Even though the router 404s (no such skill), the middleware tags the
        # response with a verification warning on the way out.
        with patch(_VERIFY_TARGET, AsyncMock(return_value="unverified")):
            r = api.client.post(
                "/api/v1/marketplace/executions",
                json={"skill_id": SKILL_UUID},
                headers=AUTH,
            )
        assert r.headers.get("X-Conduit-Verification") == "unverified"
        assert "X-Conduit-Verification-Warning" in r.headers

    def test_require_verified_blocks_with_403(self, api):
        # ?require_verified=true → middleware blocks an unverified skill before
        # the request ever reaches the router.
        with patch(_VERIFY_TARGET, AsyncMock(return_value="unverified")):
            r = api.client.post(
                "/api/v1/marketplace/executions?require_verified=true",
                json={"skill_id": SKILL_UUID},
                headers=AUTH,
            )
        assert r.status_code == 403
        assert r.json()["error"] == "skill_not_verified"

    def test_verified_skill_no_warning(self, api):
        with patch(_VERIFY_TARGET, AsyncMock(return_value="fully_verified")):
            r = api.client.post(
                "/api/v1/marketplace/executions",
                json={"skill_id": SKILL_UUID},
                headers=AUTH,
            )
        assert "X-Conduit-Verification-Warning" not in r.headers
        assert r.headers.get("X-Conduit-Verification") == "fully_verified"


class TestFederationServe:
    """The peer-serve endpoint is public, validates input, and respects the gate."""

    def test_public_and_validates(self, api):
        # No API key + a bad provider -> 422 (public: not 401; validation works).
        r = api.client.get("/api/v1/federation/attestations?provider_pubkey=nothex")
        assert r.status_code == 422

    def test_404_when_federation_disabled(self, api, monkeypatch):
        from conduit.core.config import settings
        monkeypatch.setattr(settings, "federation_enabled", False)
        r = api.client.get(f"/api/v1/federation/attestations?provider_pubkey={'a' * 64}")
        assert r.status_code == 404
