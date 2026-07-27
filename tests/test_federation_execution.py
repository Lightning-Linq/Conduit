"""Federation #3 — cross-node skill execution.

Node A brokers: it relays Node B's invoice to the consumer, the consumer pays B
DIRECTLY over Lightning, and A relays the confirm back. A never takes custody.

The whole feature is behind FEDERATION_EXECUTION_ENABLED, default false, so an
existing node's behavior (and its public surface) is unchanged until an operator
opts in.
"""

import json
import subprocess
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from conduit.core.config import Settings, settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _session_scalar(value) -> AsyncMock:
    """An AsyncSession stub whose single query resolves to ``value``."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    session.execute = AsyncMock(return_value=result)
    return session


class TestConfig:
    """The cross-node execution switch — off unless an operator opts in."""

    def test_cross_node_execution_is_off_by_default(self):
        # Assert the FIELD default, not an instance: Settings() reads the ambient
        # .env, so a dev machine that opted in would otherwise hide a bad default.
        assert Settings.model_fields["federation_execution_enabled"].default is False

    def test_setting_is_reachable_on_the_live_settings_object(self):
        assert isinstance(settings.federation_execution_enabled, bool)

    def test_it_is_independent_of_federation_enabled(self):
        """Discovery federation (#1/#2) does not imply cross-node execution (#3).

        federation_enabled defaults ON, so if the execution flag were derived from
        it, every existing node would silently start serving executions on upgrade.
        """
        fields = Settings.model_fields
        assert fields["federation_enabled"].default is True
        assert fields["federation_execution_enabled"].default is False


class TestRemoteExecutionModel:
    """A's side of the broker: the local record mapping A's execution to B's.

    Deliberately its OWN table. skill_executions.skill_id is a NOT NULL FK to
    skills.id, and a remote skill has no local Skill row — weakening that FK, or
    writing a shadow Skill row (which would then pollute discovery and be locally
    executable), are both worse than a separate table.
    """

    def test_is_exported_from_the_models_package(self):
        from conduit.models import RemoteExecution

        assert RemoteExecution.__tablename__ == "remote_executions"

    def test_columns(self):
        from conduit.models import RemoteExecution

        cols = {c.name for c in RemoteExecution.__table__.columns}
        assert {
            "id",
            "remote_skill_id",  # the skill UUID as B knows it (no local FK)
            "peer_url",  # which node hosts it
            "remote_execution_id",  # B's execution id, for the confirm callback
            "consumer_name",
            "payer_pubkey",
            "input_data",
            "amount_sats",
            "platform_fee_sats",
            "payment_hash",
            "fee_payment_hash",
            "status",
            "output_data",
            "error_message",
            "created_at",
            "updated_at",
        } <= cols

    def test_no_foreign_key_to_local_skills(self):
        """A remote skill has no local Skill row — a FK here would be unsatisfiable."""
        from conduit.models import RemoteExecution

        assert not any(c.foreign_keys for c in RemoteExecution.__table__.columns)

    def test_peer_and_remote_id_are_unique_together(self):
        """Idempotency key: one row per (peer, B's execution id), so a retried
        broker call can never create a second local row for the same purchase."""
        from conduit.models import RemoteExecution

        uniques = {
            tuple(sorted(c.columns.keys()))
            for c in RemoteExecution.__table__.constraints
            if c.__class__.__name__ == "UniqueConstraint"
        }
        assert ("peer_url", "remote_execution_id") in uniques

    def test_status_reuses_the_execution_status_enum(self):
        """Same lifecycle vocabulary as a local execution, so callers and the
        MCP/REST responses do not need a second status vocabulary."""
        from conduit.models import RemoteExecution
        from conduit.models.execution import ExecutionStatus

        assert RemoteExecution.__table__.c.status.type.enum_class is ExecutionStatus


class TestMigration:
    def test_single_head_after_the_new_revision(self):
        out = subprocess.run(
            [".venv/bin/python", "-m", "alembic", "heads"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        heads = [line for line in out.splitlines() if "(head)" in line]
        assert len(heads) == 1, f"expected exactly one alembic head, got: {out!r}"

    def test_migration_creates_the_table(self):
        migrations = (REPO_ROOT / "alembic" / "versions").glob("*.py")
        assert any(
            "remote_executions" in m.read_text() for m in migrations
        ), "no alembic revision creates remote_executions"


class TestServeExecutionRequest:
    """B's side: POST /api/v1/federation/executions.

    Unauthenticated by construction (the /federation router has no API-key
    dependency and the prefix is L402-free), so the flag IS the gate. The handler
    itself must add no money logic — it delegates to the marketplace handler so
    local and cross-node buys cannot drift apart.
    """

    def _req(self, skill_id: str | None = None):
        from conduit.api.routers.marketplace import RequestExecutionRequest

        return RequestExecutionRequest(
            skill_id=skill_id or str(uuid.uuid4()), consumer_name="node-a"
        )

    async def test_501_when_cross_node_execution_is_disabled(self, monkeypatch):
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "federation_enabled", True)
        monkeypatch.setattr(fed.settings, "federation_execution_enabled", False)

        with pytest.raises(HTTPException) as exc:
            await fed.serve_execution_request(self._req(), _session_scalar(None))
        assert exc.value.status_code == 501
        assert "Federation #3" in exc.value.detail

    async def test_404_when_federation_is_off_entirely(self, monkeypatch):
        """Matches the other serve endpoints: federation off reads as 'not here'."""
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "federation_enabled", False)
        monkeypatch.setattr(fed.settings, "federation_execution_enabled", True)

        with pytest.raises(HTTPException) as exc:
            await fed.serve_execution_request(self._req(), _session_scalar(None))
        assert exc.value.status_code == 404

    async def test_delegates_to_the_marketplace_handler_verbatim(self, monkeypatch):
        """No second implementation of invoice minting: same request, same session,
        same response body the local route would have produced."""
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "federation_enabled", True)
        monkeypatch.setattr(fed.settings, "federation_execution_enabled", True)

        seen = {}
        expected = {"execution_id": "x", "payment_request": "lnbc1...", "status": "pending_payment"}

        async def fake_local(req, session):
            seen["req"], seen["session"] = req, session
            return expected

        monkeypatch.setattr(fed, "request_skill_execution", fake_local)

        # A LOCAL skill: the onward-broker guard has to pass before delegation.
        from conduit.models.skill import Skill

        local = Skill(
            id=uuid.uuid4(), provider_name="Me", name="Local", description="d",
            category="data", price_sats=120, is_active=True,
        )
        req, session = self._req(), _session_scalar(local)
        got = await fed.serve_execution_request(req, session)

        assert got is expected
        assert seen["req"] is req and seen["session"] is session

    async def test_b_refuses_to_broker_onward(self, monkeypatch):
        """A peer must not chain A -> B -> C. B serves only skills it hosts locally.

        Exercises the REAL delegation: the marketplace resolver's cached-skill branch
        fires, so a skill B merely has cached still returns 501 rather than B
        re-brokering it. That guard is what stops loops and amplification.
        """
        from conduit.api.routers import federation as fed
        from conduit.api.routers import marketplace as mkt

        monkeypatch.setattr(fed.settings, "federation_enabled", True)
        monkeypatch.setattr(fed.settings, "federation_execution_enabled", True)
        monkeypatch.setattr(mkt.settings, "federation_enabled", True)

        async def cached(session, skill_id):
            return True

        monkeypatch.setattr(mkt, "is_cached_skill", cached)

        with pytest.raises(HTTPException) as exc:
            await fed.serve_execution_request(self._req(), _session_scalar(None))
        assert exc.value.status_code == 501
        assert "Federation #3" in exc.value.detail


class TestServeExecutionConfirm:
    """B's side: POST /api/v1/federation/executions/{id}/confirm.

    Same delegation rule as the request endpoint. Settlement verification, the
    preimage check, the webhook call, and the rating binding all stay in the
    marketplace handler — this endpoint only decides whether the door is open.
    """

    def _req(self):
        from conduit.api.routers.marketplace import ConfirmExecutionRequest

        return ConfirmExecutionRequest(payment_hash="ab" * 32, payment_preimage="cd" * 32)

    async def test_501_when_cross_node_execution_is_disabled(self, monkeypatch):
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "federation_enabled", True)
        monkeypatch.setattr(fed.settings, "federation_execution_enabled", False)

        with pytest.raises(HTTPException) as exc:
            await fed.serve_execution_confirm(
                str(uuid.uuid4()), self._req(), _session_scalar(None)
            )
        assert exc.value.status_code == 501

    async def test_404_when_federation_is_off_entirely(self, monkeypatch):
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "federation_enabled", False)
        monkeypatch.setattr(fed.settings, "federation_execution_enabled", True)

        with pytest.raises(HTTPException) as exc:
            await fed.serve_execution_confirm(
                str(uuid.uuid4()), self._req(), _session_scalar(None)
            )
        assert exc.value.status_code == 404

    async def test_delegates_to_the_marketplace_confirm_verbatim(self, monkeypatch):
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "federation_enabled", True)
        monkeypatch.setattr(fed.settings, "federation_execution_enabled", True)

        seen = {}
        expected = {"execution_id": "x", "status": "completed", "output": {"ok": True}}

        async def fake_confirm(execution_id, req, session):
            seen["id"], seen["req"], seen["session"] = execution_id, req, session
            return expected

        monkeypatch.setattr(fed, "confirm_skill_execution", fake_confirm)

        exec_id, req, session = str(uuid.uuid4()), self._req(), _session_scalar(None)
        got = await fed.serve_execution_confirm(exec_id, req, session)

        assert got is expected
        assert seen["id"] == exec_id and seen["req"] is req and seen["session"] is session


class TestRateLimits:
    """The Federation #3 endpoints are unauthenticated, so the limiter is the only
    thing standing between a hostile peer and unbounded invoice minting on B's wallet.

    Caveat this codifies: _extract_client_id returns None without an API key, so every
    anonymous caller shares ONE global counter. That caps wallet abuse (the point) but
    means one noisy peer can lock the surface for all peers. Per-IP keying needs
    trustworthy X-Forwarded-For handling, which is its own decision — see /secure-gate.
    """

    def test_request_path_is_rate_limited(self):
        from conduit.api.middleware.rate_limit import _resolve_tool

        assert (
            _resolve_tool("POST", "/api/v1/federation/executions")
            == "federation_execution_request"
        )

    def test_confirm_path_is_rate_limited(self):
        from conduit.api.middleware.rate_limit import _resolve_tool

        tool = _resolve_tool("POST", f"/api/v1/federation/executions/{uuid.uuid4()}/confirm")
        assert tool == "federation_execution_confirm"

    def test_request_limit_is_stricter_than_the_default(self):
        """Minting an invoice costs the wallet real work, so it must not fall back
        to the 30/min default that unmapped routes get."""
        from conduit.services.rate_limiter import DEFAULT_RATE_LIMIT, TOOL_RATE_LIMITS

        limit, _window = TOOL_RATE_LIMITS["federation_execution_request"]
        assert limit < DEFAULT_RATE_LIMIT

    def test_confirm_has_its_own_limit(self):
        from conduit.services.rate_limiter import TOOL_RATE_LIMITS

        assert "federation_execution_confirm" in TOOL_RATE_LIMITS

    def test_exceeding_the_request_limit_raises(self):
        from conduit.services.rate_limiter import (
            TOOL_RATE_LIMITS,
            RateLimitExceeded,
            SlidingWindowRateLimiter,
        )

        limiter = SlidingWindowRateLimiter()
        limit, _ = TOOL_RATE_LIMITS["federation_execution_request"]
        for _ in range(limit):
            limiter.check("federation_execution_request", client_id="peer-a")
        with pytest.raises(RateLimitExceeded):
            limiter.check("federation_execution_request", client_id="peer-a")


def _session_rows(rows) -> AsyncMock:
    """An AsyncSession stub whose query returns ``rows`` via .scalars().all()."""
    session = AsyncMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(rows)
    session.execute = AsyncMock(return_value=result)
    return session


def _cached(origin="peer", source_id="https://peer-a.example", created_at=100):
    from conduit.models.cached_skill import CachedSkill

    return CachedSkill(
        provider_pubkey="ab" * 32,
        skill_id=str(uuid.uuid4()),
        event_id="cd" * 32,
        event_created_at=created_at,
        origin=origin,
        source_id=source_id,
        name="Remote Indexer",
        price_sats=120,
    )


class TestResolvePeerUrl:
    """A's side: which node do we actually call?

    The load-bearing control is the allowlist. cached_skills.source_id is peer-
    supplied provenance, so trusting it alone would turn any cached listing into
    an outbound-request primitive pointed wherever the peer likes. Only a URL the
    OPERATOR listed in FEDERATION_PEERS is ever dialed.
    """

    async def test_resolves_an_allowlisted_peer(self, monkeypatch):
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(fx.settings, "federation_peers", "https://peer-a.example")
        row = _cached(origin="peer", source_id="https://peer-a.example")
        got = await fx.resolve_peer_url(_session_rows([row]), row.skill_id)
        assert got == "https://peer-a.example"

    async def test_trailing_slash_still_matches(self, monkeypatch):
        """Config and provenance disagreeing on a trailing slash is a config typo,
        not a security boundary — normalize both sides rather than silently refuse."""
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(fx.settings, "federation_peers", "https://peer-a.example/")
        row = _cached(origin="peer", source_id="https://peer-a.example")
        assert await fx.resolve_peer_url(_session_rows([row]), row.skill_id)

    async def test_refuses_a_peer_that_is_not_allowlisted(self, monkeypatch):
        """The core SSRF control: a cached row pointing somewhere else is ignored."""
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(fx.settings, "federation_peers", "https://peer-a.example")
        row = _cached(origin="peer", source_id="https://evil.example")
        with pytest.raises(fx.PeerNotResolvableError):
            await fx.resolve_peer_url(_session_rows([row]), row.skill_id)

    async def test_refuses_a_relay_discovered_listing(self, monkeypatch):
        """A relay listing carries no node base URL, so there is nothing to call.
        v1 scope: peer-origin only, with an error that says so."""
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(fx.settings, "federation_peers", "https://peer-a.example")
        row = _cached(origin="relay", source_id="wss://relay.damus.io")
        with pytest.raises(fx.PeerNotResolvableError):
            await fx.resolve_peer_url(_session_rows([row]), row.skill_id)

    async def test_refuses_an_unknown_skill(self, monkeypatch):
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(fx.settings, "federation_peers", "https://peer-a.example")
        with pytest.raises(fx.PeerNotResolvableError):
            await fx.resolve_peer_url(_session_rows([]), str(uuid.uuid4()))

    async def test_refuses_when_no_peers_are_configured(self, monkeypatch):
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(fx.settings, "federation_peers", "")
        row = _cached()
        with pytest.raises(fx.PeerNotResolvableError):
            await fx.resolve_peer_url(_session_rows([row]), row.skill_id)

    async def test_prefers_the_newest_allowlisted_listing(self, monkeypatch):
        """Two providers can publish the same skill_id coordinate. Take the freshest
        allowlisted one rather than whatever the DB happened to return first."""
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(
            fx.settings, "federation_peers", "https://peer-a.example,https://peer-b.example"
        )
        stale = _cached(source_id="https://peer-a.example", created_at=100)
        fresh = _cached(source_id="https://peer-b.example", created_at=200)
        # Rows arrive newest-first (the query orders by event_created_at desc).
        got = await fx.resolve_peer_url(_session_rows([fresh, stale]), fresh.skill_id)
        assert got == "https://peer-b.example"


# ── Node A's transport to the peer ────────────────────────────────────


class _FakeStreamResp:
    """The slice of an httpx streaming response the transport actually uses.

    ``on_read`` fires when the body is first streamed, so a test can prove the body
    was never read (e.g. Content-Length already tripped the cap).
    """

    def __init__(self, body: bytes = b"", *, headers=None, on_read=None, raises=None):
        self._body = body
        self.headers = headers or {}
        self._on_read = on_read
        self._raises = raises
        self.status_code = 200

    def raise_for_status(self):
        if self._raises is not None:
            raise self._raises

    async def aiter_bytes(self):
        if self._on_read is not None:
            self._on_read()
        for i in range(0, len(self._body), 16):
            yield self._body[i : i + 16]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _fake_httpx(route, seen=None):
    """A fake httpx.AsyncClient recording construction kwargs and stream() args."""

    class _Client:
        def __init__(self, *a, **k):
            if seen is not None:
                seen["client_kwargs"] = k

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, json=None, headers=None):
            if seen is not None:
                seen.update(method=method, url=url, json=json, headers=headers)
            return route(url)

    return _Client


def _quote(
    *,
    price=120,
    provider=118,
    fee=2,
    payment_request="lnbc1180n1pfake",
    fee_payment_request="lnbc20n1pfake",
):
    """A well-formed peer quote: 118 + 2 == the 120 sat listed price (fee-inclusive)."""
    body = {
        "execution_id": str(uuid.uuid4()),
        "skill_name": "Remote Indexer",
        "price_sats": price,
        "platform_fee_sats": fee,
        "provider_receives_sats": provider,
        "total_cost_sats": price,
        "payment_request": payment_request,
        "payment_hash": "ab" * 32,
        "status": "pending_payment",
    }
    if fee_payment_request:
        body["fee_payment_request"] = fee_payment_request
        body["fee_payment_hash"] = "cd" * 32
    return body


class TestRequestRemoteExecution:
    """A asks B to open an execution. Everything B says is untrusted."""

    @pytest.fixture(autouse=True)
    def _allow_peer(self, monkeypatch):
        """Stub the SSRF check so these tests exercise the TRANSPORT.

        validate_outbound_url does real DNS, and peer-a.example does not resolve,
        so without this every test here would pass on a DNS failure instead of on
        the behavior it names. The one test that is genuinely about SSRF restores
        the real validator.
        """
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(fx, "validate_outbound_url", lambda url: None)

    async def test_happy_path_returns_the_verified_quote(self, monkeypatch):
        import httpx

        from conduit.services import federation_execution as fx

        seen = {}
        body = json.dumps(_quote()).encode()
        monkeypatch.setattr(
            httpx, "AsyncClient", _fake_httpx(lambda url: _FakeStreamResp(body), seen)
        )

        got = await fx.request_remote_execution(
            "https://peer-a.example", skill_id=str(uuid.uuid4()), expected_price_sats=120
        )

        assert got["payment_request"] == "lnbc1180n1pfake"
        assert seen["url"] == "https://peer-a.example/api/v1/federation/executions"
        assert seen["method"] == "POST"

    async def test_never_dials_an_unsafe_peer(self, monkeypatch):
        """SSRF: the check happens BEFORE the socket, so this is asserted on the
        call counter, not inferred from a swallowed connection error."""
        import httpx

        from conduit.services import federation_execution as fx
        from conduit.services import url_safety

        # Undo the class fixture: this test IS the SSRF assertion.
        monkeypatch.setattr(fx, "validate_outbound_url", url_safety.validate_outbound_url)

        called = False

        def route(url):
            nonlocal called
            called = True
            return _FakeStreamResp(b"{}")

        monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx(route))
        # Internal address: refused on the literal, so no DNS is involved.
        with pytest.raises(fx.CrossNodeError):
            await fx.request_remote_execution(
                "http://10.0.0.1", skill_id=str(uuid.uuid4()), expected_price_sats=120
            )
        assert called is False

    async def test_redirects_are_disabled(self, monkeypatch):
        """A redirect would let a peer bounce the request to an internal host that
        validate_outbound_url already cleared the ORIGINAL url for."""
        import httpx

        from conduit.services import federation_execution as fx

        seen = {}
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _fake_httpx(lambda url: _FakeStreamResp(json.dumps(_quote()).encode()), seen),
        )
        await fx.request_remote_execution(
            "https://peer-a.example", skill_id=str(uuid.uuid4()), expected_price_sats=120
        )
        assert seen["client_kwargs"]["follow_redirects"] is False
        assert seen["headers"]["Accept-Encoding"] == "identity"

    async def test_refuses_an_oversize_body(self, monkeypatch):
        import httpx

        from conduit.services import federation_execution as fx

        over = b"x" * (fx._MAX_EXECUTION_RESPONSE_BYTES + 1)
        monkeypatch.setattr(
            httpx, "AsyncClient", _fake_httpx(lambda url: _FakeStreamResp(over))
        )
        with pytest.raises(fx.CrossNodeError):
            await fx.request_remote_execution(
                "https://peer-a.example", skill_id=str(uuid.uuid4()), expected_price_sats=120
            )

    async def test_refuses_a_compressed_body_without_decoding(self, monkeypatch):
        """Inherited from _read_body_capped: a gzip bomb decodes past the cap in a
        single chunk, so an encoded body is refused before any decode."""
        import httpx

        from conduit.services import federation_execution as fx

        read = False

        def on_read():
            nonlocal read
            read = True

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _fake_httpx(
                lambda url: _FakeStreamResp(
                    b"{}", headers={"content-encoding": "gzip"}, on_read=on_read
                )
            ),
        )
        with pytest.raises(fx.CrossNodeError):
            await fx.request_remote_execution(
                "https://peer-a.example", skill_id=str(uuid.uuid4()), expected_price_sats=120
            )
        assert read is False

    async def test_peer_error_status_is_typed(self, monkeypatch):
        import httpx

        from conduit.services import federation_execution as fx

        boom = httpx.HTTPStatusError("500", request=None, response=None)
        monkeypatch.setattr(
            httpx, "AsyncClient", _fake_httpx(lambda url: _FakeStreamResp(b"{}", raises=boom))
        )
        with pytest.raises(fx.PeerRejectedError):
            await fx.request_remote_execution(
                "https://peer-a.example", skill_id=str(uuid.uuid4()), expected_price_sats=120
            )

    async def test_missing_execution_id_is_invalid(self, monkeypatch):
        import httpx

        from conduit.services import federation_execution as fx

        q = _quote()
        del q["execution_id"]
        monkeypatch.setattr(
            httpx, "AsyncClient", _fake_httpx(lambda url: _FakeStreamResp(json.dumps(q).encode()))
        )
        with pytest.raises(fx.PeerResponseInvalidError):
            await fx.request_remote_execution(
                "https://peer-a.example", skill_id=str(uuid.uuid4()), expected_price_sats=120
            )


class TestQuoteVerification:
    """Bait-and-switch: the listing, the peer's claim, and the INVOICE must agree.

    Only the encoded invoice amount binds the consumer's wallet, and only the
    listing price is what the agent decided to buy against. Any disagreement is
    refused before the consumer ever sees a payment request.
    """

    @pytest.fixture(autouse=True)
    def _allow_peer(self, monkeypatch):
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(fx, "validate_outbound_url", lambda url: None)

    async def _request(self, monkeypatch, quote, expected=120):
        import httpx

        from conduit.services import federation_execution as fx

        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _fake_httpx(lambda url: _FakeStreamResp(json.dumps(quote).encode())),
        )
        return await fx.request_remote_execution(
            "https://peer-a.example", skill_id=str(uuid.uuid4()), expected_price_sats=expected
        )

    async def test_refuses_an_invoice_richer_than_the_claim(self, monkeypatch):
        """The headline attack: JSON says 118 sats, the invoice encodes 500,000."""
        from conduit.services import federation_execution as fx

        with pytest.raises(fx.PeerResponseInvalidError):
            await self._request(
                monkeypatch, _quote(payment_request="lnbc5m1pfake")  # 500,000 sats
            )

    async def test_refuses_a_quote_that_exceeds_the_listed_price(self, monkeypatch):
        """Peer is internally consistent but ignores what the catalog advertised."""
        from conduit.services import federation_execution as fx

        with pytest.raises(fx.PeerResponseInvalidError):
            await self._request(
                monkeypatch,
                _quote(price=9000, provider=8900, fee=100, payment_request="lnbc89u1pfake"),
                expected=120,
            )

    async def test_refuses_an_inflated_fee_invoice(self, monkeypatch):
        """Fee-inclusive pricing: an honest provider invoice plus a fat fee invoice
        still overcharges the buyer, so the SPLIT is verified, not just the total."""
        from conduit.services import federation_execution as fx

        with pytest.raises(fx.PeerResponseInvalidError):
            await self._request(
                monkeypatch, _quote(fee_payment_request="lnbc1m1pfake")  # 100,000 sats
            )

    async def test_refuses_an_amountless_invoice(self, monkeypatch):
        """No encoded amount means the quote is unverifiable and some wallets will
        let the payer (or a default) choose. Fail closed."""
        from conduit.services import federation_execution as fx

        with pytest.raises(fx.PeerResponseInvalidError):
            await self._request(monkeypatch, _quote(payment_request="lnbc1pfake"))

    async def test_refuses_a_split_that_does_not_sum_to_the_price(self, monkeypatch):
        from conduit.services import federation_execution as fx

        with pytest.raises(fx.PeerResponseInvalidError):
            await self._request(
                monkeypatch, _quote(price=120, provider=118, fee=50), expected=120
            )

    async def test_accepts_a_free_skill_with_no_invoice(self, monkeypatch):
        """price 0 mints no invoices at all, so there is nothing to compare."""
        q = _quote(price=0, provider=0, fee=0, payment_request=None, fee_payment_request=None)
        q["payment_request"] = None
        q["payment_hash"] = None
        q["status"] = "completed"
        got = await self._request(monkeypatch, q, expected=0)
        assert got["status"] == "completed"


class TestConfirmRemoteExecution:
    """A relays the consumer's preimage to B and hands back B's output."""

    @pytest.fixture(autouse=True)
    def _allow_peer(self, monkeypatch):
        from conduit.services import federation_execution as fx

        monkeypatch.setattr(fx, "validate_outbound_url", lambda url: None)

    def _result(self):
        return {
            "execution_id": str(uuid.uuid4()),
            "status": "completed",
            "output": {"rows": 42},
            "fee_settled": True,
        }

    async def test_posts_to_the_peer_confirm_path(self, monkeypatch):
        import httpx

        from conduit.services import federation_execution as fx

        seen = {}
        remote_id = str(uuid.uuid4())
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _fake_httpx(lambda url: _FakeStreamResp(json.dumps(self._result()).encode()), seen),
        )

        got = await fx.confirm_remote_execution(
            "https://peer-a.example",
            remote_id,
            payment_hash="ab" * 32,
            payment_preimage="cd" * 32,
        )

        assert got["status"] == "completed"
        assert seen["url"] == (
            f"https://peer-a.example/api/v1/federation/executions/{remote_id}/confirm"
        )
        assert seen["json"] == {"payment_hash": "ab" * 32, "payment_preimage": "cd" * 32}

    async def test_uses_a_longer_timeout_than_the_request_call(self, monkeypatch):
        """B runs the provider webhook inside confirm, and that has its own 30s
        budget — timing out at 15s would abandon a purchase the consumer PAID for."""
        import httpx

        from conduit.services import federation_execution as fx

        seen = {}
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            _fake_httpx(lambda url: _FakeStreamResp(json.dumps(self._result()).encode()), seen),
        )
        await fx.confirm_remote_execution(
            "https://peer-a.example",
            str(uuid.uuid4()),
            payment_hash="ab" * 32,
            payment_preimage="cd" * 32,
        )
        assert seen["client_kwargs"]["timeout"] > fx._REQUEST_TIMEOUT

    async def test_refuses_an_execution_id_that_is_not_a_uuid(self, monkeypatch):
        """The id is interpolated into a URL path, and it ORIGINATED at the peer.
        A value like '../../admin/reset-demo' would aim the confirm at a different
        endpoint, so the shape is enforced instead of trusted."""
        import httpx

        from conduit.services import federation_execution as fx

        called = False

        def route(url):
            nonlocal called
            called = True
            return _FakeStreamResp(b"{}")

        monkeypatch.setattr(httpx, "AsyncClient", _fake_httpx(route))
        with pytest.raises(fx.PeerResponseInvalidError):
            await fx.confirm_remote_execution(
                "https://peer-a.example",
                "../../admin/reset-demo",
                payment_hash="ab" * 32,
                payment_preimage="cd" * 32,
            )
        assert called is False

    async def test_a_quote_with_a_traversal_execution_id_is_refused(self, monkeypatch):
        """Same guard, one step earlier: the id is rejected when the peer first
        sends it, so it never reaches the database."""
        import httpx

        from conduit.services import federation_execution as fx

        q = _quote()
        q["execution_id"] = "../../admin/reset-demo"
        monkeypatch.setattr(
            httpx, "AsyncClient", _fake_httpx(lambda url: _FakeStreamResp(json.dumps(q).encode()))
        )
        with pytest.raises(fx.PeerResponseInvalidError):
            await fx.request_remote_execution(
                "https://peer-a.example", skill_id=str(uuid.uuid4()), expected_price_sats=120
            )

    async def test_peer_refusal_is_typed(self, monkeypatch):
        """B returning 402 (invoice not settled) must surface as a peer error, not
        a generic 500 from A."""
        import httpx

        from conduit.services import federation_execution as fx

        boom = httpx.HTTPStatusError("402", request=None, response=None)
        monkeypatch.setattr(
            httpx, "AsyncClient", _fake_httpx(lambda url: _FakeStreamResp(b"{}", raises=boom))
        )
        with pytest.raises(fx.PeerRejectedError):
            await fx.confirm_remote_execution(
                "https://peer-a.example",
                str(uuid.uuid4()),
                payment_hash="ab" * 32,
                payment_preimage="cd" * 32,
            )

    async def test_refuses_an_oversize_output(self, monkeypatch):
        """The output lands in a JSONB column, so the cap is what stops a peer from
        writing an unbounded row."""
        import httpx

        from conduit.services import federation_execution as fx

        over = b"x" * (fx._MAX_EXECUTION_RESPONSE_BYTES + 1)
        monkeypatch.setattr(
            httpx, "AsyncClient", _fake_httpx(lambda url: _FakeStreamResp(over))
        )
        with pytest.raises(fx.PeerResponseInvalidError):
            await fx.confirm_remote_execution(
                "https://peer-a.example",
                str(uuid.uuid4()),
                payment_hash="ab" * 32,
                payment_preimage="cd" * 32,
            )


# ── A's REST surface ──────────────────────────────────────────────────


def _broker_session():
    """A session stub for the broker path: the skill is not local, add() records."""
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None  # no local Skill row
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


class TestBrokerRequestPath:
    """A's POST /marketplace/executions when the skill lives on a peer."""

    def _req(self, skill_id=None):
        from conduit.api.routers.marketplace import RequestExecutionRequest

        return RequestExecutionRequest(
            skill_id=skill_id or str(uuid.uuid4()),
            consumer_name="buyer",
            input_data={"q": "hello"},
        )

    @pytest.fixture(autouse=True)
    def _remote_skill(self, monkeypatch):
        """The requested skill is cached from a peer, not hosted locally."""
        from conduit.api.routers import marketplace as mkt

        monkeypatch.setattr(mkt.settings, "federation_enabled", True)

        async def cached(session, skill_id):
            return True

        monkeypatch.setattr(mkt, "is_cached_skill", cached)

    async def test_still_501_when_cross_node_execution_is_disabled(self, monkeypatch):
        """Flag off must behave exactly as it does today."""
        from conduit.api.routers import marketplace as mkt

        monkeypatch.setattr(mkt.settings, "federation_execution_enabled", False)

        with pytest.raises(HTTPException) as exc:
            await mkt.request_skill_execution(self._req(), _broker_session())
        assert exc.value.status_code == 501
        assert "Federation #3" in exc.value.detail

    def _wire_broker(self, monkeypatch, *, quote=None, listing_price=120, raises=None):
        from conduit.api.routers import marketplace as mkt
        from conduit.models.cached_skill import CachedSkill

        monkeypatch.setattr(mkt.settings, "federation_execution_enabled", True)
        seen = {}

        async def fake_resolve(session, skill_id):
            return "https://peer-a.example"

        async def fake_listing(session, skill_id):
            return CachedSkill(
                provider_pubkey="ab" * 32,
                skill_id=skill_id,
                event_id="cd" * 32,
                event_created_at=1,
                origin="peer",
                source_id="https://peer-a.example",
                name="Remote Indexer",
                price_sats=listing_price,
            )

        async def fake_request(peer_url, **kwargs):
            seen.update(peer_url=peer_url, **kwargs)
            if raises is not None:
                raise raises
            return quote if quote is not None else _quote()

        monkeypatch.setattr(mkt, "resolve_peer_url", fake_resolve)
        monkeypatch.setattr(mkt, "get_cached_listing", fake_listing)
        monkeypatch.setattr(mkt, "request_remote_execution", fake_request)
        return seen

    async def test_returns_the_peers_invoice(self, monkeypatch):
        from conduit.api.routers import marketplace as mkt

        self._wire_broker(monkeypatch)
        got = await mkt.request_skill_execution(self._req(), _broker_session())

        assert got["payment_request"] == "lnbc1180n1pfake"
        assert got["origin"] == "peer"
        assert got["host_node"] == "https://peer-a.example"

    async def test_quotes_against_the_cached_listing_price(self, monkeypatch):
        """The bait-and-switch check is only as good as the price fed into it, and
        that price must come from the signed listing, never from the peer's reply."""
        from conduit.api.routers import marketplace as mkt

        seen = self._wire_broker(monkeypatch, listing_price=120)
        await mkt.request_skill_execution(self._req(), _broker_session())
        assert seen["expected_price_sats"] == 120

    async def test_persists_exactly_one_remote_execution(self, monkeypatch):
        from conduit.api.routers import marketplace as mkt
        from conduit.models import RemoteExecution

        self._wire_broker(monkeypatch)
        session = _broker_session()
        req = self._req()
        await mkt.request_skill_execution(req, session)

        assert session.add.call_count == 1
        row = session.add.call_args[0][0]
        assert isinstance(row, RemoteExecution)
        assert row.peer_url == "https://peer-a.example"
        assert row.remote_skill_id == req.skill_id
        assert row.input_data == {"q": "hello"}

    async def test_execution_id_is_the_local_row_not_the_peers(self, monkeypatch):
        """The consumer confirms against THIS node, so it must get this node's id."""
        from conduit.api.routers import marketplace as mkt

        q = _quote()
        self._wire_broker(monkeypatch, quote=q)
        session = _broker_session()
        got = await mkt.request_skill_execution(self._req(), session)

        row = session.add.call_args[0][0]
        assert got["execution_id"] == str(row.id)
        assert got["execution_id"] != q["execution_id"]
        assert row.remote_execution_id == q["execution_id"]

    async def test_unresolvable_peer_is_a_404(self, monkeypatch):
        from conduit.api.routers import marketplace as mkt
        from conduit.services.federation_execution import PeerNotResolvableError

        self._wire_broker(monkeypatch, raises=PeerNotResolvableError("nope"))
        with pytest.raises(HTTPException) as exc:
            await mkt.request_skill_execution(self._req(), _broker_session())
        assert exc.value.status_code == 404

    async def test_peer_failure_is_a_502(self, monkeypatch):
        from conduit.api.routers import marketplace as mkt
        from conduit.services.federation_execution import PeerRejectedError

        self._wire_broker(monkeypatch, raises=PeerRejectedError("peer down"))
        with pytest.raises(HTTPException) as exc:
            await mkt.request_skill_execution(self._req(), _broker_session())
        assert exc.value.status_code == 502

    async def test_a_bad_quote_writes_no_row(self, monkeypatch):
        """The bait-and-switch refusal must leave nothing behind — no half-open
        purchase for a consumer to stumble into confirming."""
        from conduit.api.routers import marketplace as mkt
        from conduit.services.federation_execution import PeerResponseInvalidError

        self._wire_broker(monkeypatch, raises=PeerResponseInvalidError("invoice inflated"))
        session = _broker_session()
        with pytest.raises(HTTPException) as exc:
            await mkt.request_skill_execution(self._req(), session)
        assert exc.value.status_code == 502
        assert session.add.call_count == 0


def _session_seq(values):
    """A session stub returning ``values`` from successive execute() calls."""
    session = AsyncMock()

    def _result(v):
        r = MagicMock()
        r.scalar_one_or_none.return_value = v
        return r

    session.execute = AsyncMock(side_effect=[_result(v) for v in values])
    session.add = MagicMock()
    session.commit = AsyncMock()
    return session


def _remote_row(status=None, payment_hash=None):
    from conduit.models import RemoteExecution
    from conduit.models.execution import ExecutionStatus

    row = RemoteExecution(
        remote_skill_id=str(uuid.uuid4()),
        peer_url="https://peer-a.example",
        remote_execution_id=str(uuid.uuid4()),
        consumer_name="buyer",
        amount_sats=120,
        platform_fee_sats=2,
        payment_hash=payment_hash or ("ab" * 32),
        status=status or ExecutionStatus.PENDING_PAYMENT,
    )
    row.id = uuid.uuid4()
    return row


class TestBrokerConfirmPath:
    """A's POST /marketplace/executions/{id}/confirm for a brokered purchase."""

    # A preimage and the hash it actually produces, so the local SHA256 check passes.
    PREIMAGE = "11" * 32

    @property
    def hash_hex(self):
        import hashlib

        return hashlib.sha256(bytes.fromhex(self.PREIMAGE)).hexdigest()

    def _req(self, payment_hash=None, preimage=None):
        from conduit.api.routers.marketplace import ConfirmExecutionRequest

        return ConfirmExecutionRequest(
            payment_hash=payment_hash or self.hash_hex,
            payment_preimage=preimage or self.PREIMAGE,
        )

    def _wire_peer(self, monkeypatch, *, result=None, raises=None):
        from conduit.api.routers import marketplace as mkt

        seen = {}

        async def fake_confirm(peer_url, remote_id, **kwargs):
            seen.update(peer_url=peer_url, remote_id=remote_id, **kwargs)
            if raises is not None:
                raise raises
            return result if result is not None else {
                "execution_id": str(uuid.uuid4()),
                "status": "completed",
                "output": {"rows": 42},
                "federation": {"provider_binding_sig": "sig"},
            }

        monkeypatch.setattr(mkt, "confirm_remote_execution", fake_confirm)
        return seen

    async def test_local_execution_wins(self, monkeypatch):
        """A local execution id must never be resolved against the remote table."""
        from conduit.api.routers import marketplace as mkt

        called = self._wire_peer(monkeypatch)
        local = _remote_row()  # stands in for any row; the local query returns it first

        # The local branch will fail later (no skill/wallet), but the point is that
        # the peer was never consulted for an id that resolved locally.
        with pytest.raises(Exception):
            await mkt.confirm_skill_execution(str(uuid.uuid4()), self._req(), _session_seq([local]))
        assert called == {}

    async def test_forwards_a_remote_confirm_to_the_peer(self, monkeypatch):
        from conduit.api.routers import marketplace as mkt

        row = _remote_row(payment_hash=self.hash_hex)
        seen = self._wire_peer(monkeypatch)
        got = await mkt.confirm_skill_execution(
            str(row.id), self._req(), _session_seq([None, row])
        )

        assert seen["peer_url"] == "https://peer-a.example"
        assert seen["remote_id"] == row.remote_execution_id
        assert seen["payment_preimage"] == self.PREIMAGE
        assert got["output"] == {"rows": 42}
        assert got["origin"] == "peer"
        assert got["host_node"] == "https://peer-a.example"

    async def test_relays_the_peers_federation_block(self, monkeypatch):
        """The peer mints the payer-binding signature, so relaying it is what lets
        the consumer publish a federated rating for a cross-node purchase."""
        from conduit.api.routers import marketplace as mkt

        row = _remote_row(payment_hash=self.hash_hex)
        self._wire_peer(monkeypatch)
        got = await mkt.confirm_skill_execution(
            str(row.id), self._req(), _session_seq([None, row])
        )
        assert got["federation"] == {"provider_binding_sig": "sig"}

    async def test_unknown_id_is_404(self, monkeypatch):
        from conduit.api.routers import marketplace as mkt

        self._wire_peer(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            await mkt.confirm_skill_execution(
                str(uuid.uuid4()), self._req(), _session_seq([None, None])
            )
        assert exc.value.status_code == 404

    async def test_bad_preimage_never_reaches_the_peer(self, monkeypatch):
        """Checked locally first: SHA256(preimage) must equal the stored hash, so a
        junk confirm cannot be used to hammer the peer."""
        from conduit.api.routers import marketplace as mkt

        row = _remote_row(payment_hash=self.hash_hex)
        seen = self._wire_peer(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            await mkt.confirm_skill_execution(
                str(row.id),
                self._req(preimage="22" * 32),
                _session_seq([None, row]),
            )
        assert exc.value.status_code == 400
        assert seen == {}

    async def test_payment_hash_must_match_the_row(self, monkeypatch):
        from conduit.api.routers import marketplace as mkt

        row = _remote_row(payment_hash="ff" * 32)
        seen = self._wire_peer(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            await mkt.confirm_skill_execution(
                str(row.id), self._req(), _session_seq([None, row])
            )
        assert exc.value.status_code == 400
        assert seen == {}

    async def test_terminal_status_conflicts(self, monkeypatch):
        from conduit.api.routers import marketplace as mkt
        from conduit.models.execution import ExecutionStatus

        row = _remote_row(status=ExecutionStatus.COMPLETED, payment_hash=self.hash_hex)
        self._wire_peer(monkeypatch)
        with pytest.raises(HTTPException) as exc:
            await mkt.confirm_skill_execution(
                str(row.id), self._req(), _session_seq([None, row])
            )
        assert exc.value.status_code == 409

    async def test_peer_failure_leaves_the_purchase_retryable(self, monkeypatch):
        """The consumer has ALREADY paid the peer. Marking this FAILED would strand
        them, so a peer-side refusal must leave the row confirmable again."""
        from conduit.api.routers import marketplace as mkt
        from conduit.models.execution import ExecutionStatus
        from conduit.services.federation_execution import PeerRejectedError

        row = _remote_row(payment_hash=self.hash_hex)
        self._wire_peer(monkeypatch, raises=PeerRejectedError("peer down"))
        with pytest.raises(HTTPException) as exc:
            await mkt.confirm_skill_execution(
                str(row.id), self._req(), _session_seq([None, row])
            )
        assert exc.value.status_code == 502
        assert row.status == ExecutionStatus.PENDING_PAYMENT


# ── MCP surface ───────────────────────────────────────────────────────


def _mcp_session_factory(session):
    """A factory whose `async with factory() as s:` yields ``session``."""
    ctx = AsyncMock()
    ctx.__aenter__.return_value = session
    ctx.__aexit__.return_value = False
    return MagicMock(return_value=ctx)


class TestMcpBrokerRequest:
    """MCP is the primary agent interface, so it must broker exactly like REST does.

    A drift here would be worse than a REST drift: agents reach Conduit through MCP
    first, and a cross-node buy that only works over HTTP is invisible to them.
    """

    @pytest.fixture(autouse=True)
    def _remote_skill(self, monkeypatch):
        import conduit.mcp_server as mcp

        # mcp_server imports settings INSIDE the handler, so patch the singleton.
        monkeypatch.setattr(settings, "federation_enabled", True)

        async def cached(session, skill_id):
            return True

        async def not_local(session, skill_id):
            return None

        monkeypatch.setattr(mcp, "is_cached_skill", cached)
        monkeypatch.setattr(mcp, "_find_skill_by_id", not_local)
        monkeypatch.setattr(
            mcp, "async_session_factory", _mcp_session_factory(_broker_session())
        )

    async def test_still_refuses_when_cross_node_execution_is_disabled(self, monkeypatch):
        import conduit.mcp_server as mcp

        monkeypatch.setattr(settings, "federation_execution_enabled", False)
        out = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})
        assert "Federation #3" in out[0].text

    async def test_brokers_and_reports_the_peers_invoice(self, monkeypatch):
        import conduit.mcp_server as mcp
        from conduit.models.cached_skill import CachedSkill

        monkeypatch.setattr(settings, "federation_execution_enabled", True)

        async def fake_resolve(session, skill_id):
            return "https://peer-a.example"

        async def fake_listing(session, skill_id):
            return CachedSkill(
                provider_pubkey="ab" * 32, skill_id=skill_id, event_id="cd" * 32,
                event_created_at=1, origin="peer", source_id="https://peer-a.example",
                name="Remote Indexer", price_sats=120,
            )

        async def fake_request(peer_url, **kwargs):
            return _quote()

        monkeypatch.setattr(mcp, "resolve_peer_url", fake_resolve)
        monkeypatch.setattr(mcp, "get_cached_listing", fake_listing)
        monkeypatch.setattr(mcp, "request_remote_execution", fake_request)

        out = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})
        text = out[0].text
        assert "lnbc1180n1pfake" in text
        assert "peer-a.example" in text  # the agent is told WHICH node it is buying from

    async def test_a_refused_quote_is_reported_not_raised(self, monkeypatch):
        """MCP tools answer with text; an unhandled exception would surface as a
        broken tool call instead of a usable message."""
        import conduit.mcp_server as mcp
        from conduit.services.federation_execution import PeerResponseInvalidError

        monkeypatch.setattr(settings, "federation_execution_enabled", True)

        async def fake_resolve(session, skill_id):
            return "https://peer-a.example"

        async def boom(session, skill_id):
            raise PeerResponseInvalidError("invoice encodes 500000 sats but the peer quoted 118")

        monkeypatch.setattr(mcp, "resolve_peer_url", fake_resolve)
        monkeypatch.setattr(mcp, "get_cached_listing", boom)

        out = await mcp._request_skill_execution({"skill_id": str(uuid.uuid4())})
        assert "500000" in out[0].text or "could not" in out[0].text.lower()


class TestMcpBrokerConfirm:
    PREIMAGE = "11" * 32

    @property
    def hash_hex(self):
        import hashlib

        return hashlib.sha256(bytes.fromhex(self.PREIMAGE)).hexdigest()

    async def test_forwards_a_remote_confirm(self, monkeypatch):
        import conduit.mcp_server as mcp

        row = _remote_row(payment_hash=self.hash_hex)
        session = _session_seq([None, row])
        monkeypatch.setattr(mcp, "async_session_factory", _mcp_session_factory(session))

        seen = {}

        async def fake_confirm(peer_url, remote_id, **kwargs):
            seen.update(peer_url=peer_url, remote_id=remote_id, **kwargs)
            return {"status": "completed", "output": {"rows": 42}}

        monkeypatch.setattr(mcp, "confirm_remote_execution", fake_confirm)

        out = await mcp._confirm_skill_execution(
            {"execution_id": str(row.id), "payment_preimage": self.PREIMAGE}
        )
        assert seen["remote_id"] == row.remote_execution_id
        assert "42" in out[0].text

    async def test_unknown_id_still_reports_not_found(self, monkeypatch):
        import conduit.mcp_server as mcp

        monkeypatch.setattr(
            mcp, "async_session_factory", _mcp_session_factory(_session_seq([None, None]))
        )
        out = await mcp._confirm_skill_execution(
            {"execution_id": str(uuid.uuid4()), "payment_preimage": self.PREIMAGE}
        )
        assert "not found" in out[0].text.lower()


class TestCrossNodeRespectsVerificationPolicy:
    """REQUIRE_VERIFIED_SKILLS must hold for peers too.

    Found at /secure-gate: VerificationEnforcementMiddleware matches the exact path
    /api/v1/marketplace/executions (verification.py:63), so the cross-node endpoint
    is a different path and slipped past it. An operator who blocks unverified skills
    means it for every buyer, not just local ones.
    """

    def _req(self, skill_id=None):
        from conduit.api.routers.marketplace import RequestExecutionRequest

        return RequestExecutionRequest(
            skill_id=skill_id or str(uuid.uuid4()), consumer_name="node-a"
        )

    def _skill(self, verification_status):
        from conduit.models.skill import Skill

        return Skill(
            id=uuid.uuid4(), provider_name="Me", name="Local", description="d",
            category="data", price_sats=120, is_active=True,
            verification_status=verification_status,
        )

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "federation_enabled", True)
        monkeypatch.setattr(fed.settings, "federation_execution_enabled", True)

    async def test_unverified_skill_is_blocked_when_policy_requires_verification(
        self, monkeypatch
    ):
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "require_verified_skills", True)

        called = False

        async def fake_local(req, session):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(fed, "request_skill_execution", fake_local)

        with pytest.raises(HTTPException) as exc:
            await fed.serve_execution_request(
                self._req(), _session_scalar(self._skill("unverified"))
            )
        assert exc.value.status_code == 403
        assert called is False  # refused before any invoice is minted

    async def test_verified_skill_still_sells_cross_node(self, monkeypatch):
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "require_verified_skills", True)

        async def fake_local(req, session):
            return {"execution_id": "x"}

        monkeypatch.setattr(fed, "request_skill_execution", fake_local)
        got = await fed.serve_execution_request(
            self._req(), _session_scalar(self._skill("domain_verified"))
        )
        assert got == {"execution_id": "x"}

    async def test_unverified_sells_when_the_policy_is_off(self, monkeypatch):
        """Default posture is unchanged: unverified skills are sellable."""
        from conduit.api.routers import federation as fed

        monkeypatch.setattr(fed.settings, "require_verified_skills", False)

        async def fake_local(req, session):
            return {"execution_id": "x"}

        monkeypatch.setattr(fed, "request_skill_execution", fake_local)
        got = await fed.serve_execution_request(
            self._req(), _session_scalar(self._skill("unverified"))
        )
        assert got == {"execution_id": "x"}
