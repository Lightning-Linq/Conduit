"""Cross-node execution — the broker side (Federation #3).

Discovery is federated (#2), so this node's agents can see skills hosted by peers.
This module lets them BUY one: it resolves which peer hosts the skill, asks that
peer to open an execution, and later relays the confirm. The consumer pays the
peer's invoice directly over Lightning, so this node is a broker and never a
custodian — the only thing that crosses the wire is a payment request.

Trust model. A peer is untrusted infrastructure, exactly as in the catalog and
reputation transports:

- The target host comes from the OPERATOR's FEDERATION_PEERS allowlist, never from
  the cached listing alone. `cached_skills.source_id` is peer-supplied provenance;
  treating it as an address would turn any cached row into an outbound-request
  primitive aimed wherever the peer likes.
- Every request is SSRF-validated, sent with redirects disabled, and read under a
  hard size cap (shared with the catalog transport, which already survived two
  security gates).
- The peer's invoice is checked against the price the listing advertised before the
  consumer ever sees it, so a compromised peer cannot swap a cheap listing for an
  expensive invoice.
- The peer's output is data: capped, stored, returned. It never drives control flow.
"""

from __future__ import annotations

import json
import uuid

import httpx
from sqlalchemy import select

from conduit.core.config import settings
from conduit.models.cached_skill import CachedSkill
from conduit.services.catalog_transport import _read_body_capped
from conduit.services.nwc import _parse_bolt11_amount
from conduit.services.url_safety import UnsafeURLError, validate_outbound_url

_EXECUTIONS_PATH = "/api/v1/federation/executions"

# Same ceiling the catalog transport uses. Execution outputs can legitimately carry
# a base64 artifact (qr-generate, image-convert, pdf-text all return one inline), so
# a tight cap would break real skills; 8 MiB bounds memory without doing that.
_MAX_EXECUTION_RESPONSE_BYTES = 8 * 1024 * 1024

# Opening an execution is a quick DB + invoice round-trip on the peer.
_REQUEST_TIMEOUT = 15.0

# Confirm is slow on purpose: the peer verifies settlement and then runs the
# provider webhook, which has its own 30s budget. Timing out early would abandon a
# purchase the consumer has ALREADY paid for, so allow the webhook to finish.
_CONFIRM_TIMEOUT = 60.0


class CrossNodeError(Exception):
    """Base for every cross-node execution failure."""


class PeerNotResolvableError(CrossNodeError):
    """No allowlisted peer hosts this skill, so there is nothing to call."""


class PeerRejectedError(CrossNodeError):
    """The peer refused the request, or could not be reached."""


class PeerResponseInvalidError(CrossNodeError):
    """The peer answered, but the answer cannot be trusted or acted on."""


def _normalize_peer(url: str) -> str:
    """Canonical form for comparing a configured peer to a listing's provenance.

    Only strips a trailing slash and normalizes case: a slash mismatch between
    FEDERATION_PEERS and the serve URL is a config typo, not a security boundary.
    Anything beyond that (host, scheme, port, path) must match exactly, and the
    result still goes through validate_outbound_url before any socket is opened.
    """
    return url.strip().rstrip("/").lower()


async def resolve_peer_url(session, skill_id: str) -> str:
    """Return the allowlisted peer base URL hosting ``skill_id``.

    Raises PeerNotResolvableError when the skill is unknown, was discovered from a relay
    (a relay listing carries no node address, so there is nobody to call), or names
    a peer this operator has not allowlisted.
    """
    allowlist = {_normalize_peer(u) for u in settings.federation_peer_list}
    if not allowlist:
        raise PeerNotResolvableError(
            "No federation peers are configured, so cross-node execution has no "
            "target. Set FEDERATION_PEERS to the nodes you are willing to buy from."
        )

    result = await session.execute(
        select(CachedSkill)
        .where(CachedSkill.skill_id == skill_id)
        .order_by(CachedSkill.event_created_at.desc())
    )
    rows = list(result.scalars().all())
    if not rows:
        raise PeerNotResolvableError(f"{skill_id} is not a known remote skill on this node")

    # Newest listing first, so a refreshed coordinate wins over a stale one.
    saw_relay_only = True
    for row in rows:
        if row.origin != "peer" or not row.source_id:
            continue
        saw_relay_only = False
        candidate = _normalize_peer(row.source_id)
        if candidate in allowlist:
            return candidate

    if saw_relay_only:
        raise PeerNotResolvableError(
            f"{skill_id} was discovered from a Nostr relay, which advertises no node "
            "address. Cross-node execution currently requires the hosting node to be "
            "one of your configured FEDERATION_PEERS."
        )
    raise PeerNotResolvableError(
        f"{skill_id} is hosted by a node that is not in FEDERATION_PEERS. Add it "
        "explicitly if you are willing to buy from it."
    )


async def get_cached_listing(session, skill_id: str) -> CachedSkill | None:
    """The freshest cached listing for ``skill_id``, or None.

    The broker quotes against THIS row's price, not against anything the peer says
    at purchase time. The row came from a signature-verified kind-38383 event, so it
    is the closest thing to a commitment the seller has made.
    """
    result = await session.execute(
        select(CachedSkill)
        .where(CachedSkill.skill_id == skill_id)
        .order_by(CachedSkill.event_created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


# ── Talking to the peer ───────────────────────────────────────────────


async def _post_to_peer(peer_url: str, path: str, payload: dict, *, timeout: float) -> dict:
    """POST JSON to a peer and return the decoded body, under every transport guard.

    The guards are the catalog transport's, reused rather than re-derived:
    SSRF-validate before opening a socket, refuse redirects (a 302 would escape the
    validation the ORIGINAL url passed), demand an identity encoding, and read the
    body under a hard size cap.
    """
    try:
        validate_outbound_url(peer_url)
    except UnsafeURLError as e:
        raise PeerRejectedError(f"peer url is not safe to dial: {e}") from None

    url = peer_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async with client.stream(
                "POST", url, json=payload, headers={"Accept-Encoding": "identity"}
            ) as resp:
                resp.raise_for_status()
                body = await _read_body_capped(resp, _MAX_EXECUTION_RESPONSE_BYTES)
    except httpx.HTTPStatusError as e:
        raise PeerRejectedError(f"peer refused the request: {e}") from None
    except ValueError as e:
        # _read_body_capped's refusal (oversize or compressed) — a hostile or broken
        # peer, not a transport failure.
        raise PeerResponseInvalidError(str(e)) from None
    except Exception as e:
        raise PeerRejectedError(f"could not reach peer: {e}") from None

    try:
        decoded = json.loads(body)
    except Exception:
        raise PeerResponseInvalidError("peer response was not valid JSON") from None
    if not isinstance(decoded, dict):
        raise PeerResponseInvalidError("peer response was not a JSON object")
    return decoded


def _assert_execution_id(execution_id) -> str:
    """Assert the peer's execution id is a plain UUID.

    This value is chosen by the PEER and later interpolated into a URL path when we
    confirm, so an id like ``../../admin/reset-demo`` would aim the confirm at some
    other endpoint on that peer. Conduit always issues ``str(uuid4())``, so requiring
    that shape costs nothing and removes the injection outright — no escaping to get
    subtly wrong. Enforced on arrival, so a malformed id never reaches the database.
    """
    if not execution_id or not isinstance(execution_id, str):
        raise PeerResponseInvalidError("peer response has no execution_id")
    try:
        uuid.UUID(execution_id)
    except ValueError:
        raise PeerResponseInvalidError(
            f"peer execution_id is not a UUID: {execution_id!r}"
        ) from None
    return execution_id


def _verify_quote(quote: dict, expected_price_sats: int) -> None:
    """Refuse a quote unless the listing, the peer's claim, and the INVOICES agree.

    Three sources have to line up. ``expected_price_sats`` is what the signed catalog
    listing advertised and therefore what the buying agent decided against. The
    peer's JSON is its claim. The bolt11 amount is the only one that actually binds
    the consumer's wallet. A peer that disagrees with itself, or with the listing, is
    refused before the consumer ever sees a payment request — an agent will happily
    pay an invoice nobody read.
    """
    _assert_execution_id(quote.get("execution_id"))

    claimed_price = quote.get("price_sats")
    claimed_fee = quote.get("platform_fee_sats") or 0
    claimed_provider = quote.get("provider_receives_sats")
    if not isinstance(claimed_price, int) or not isinstance(claimed_provider, int):
        raise PeerResponseInvalidError("peer quote is missing its amounts")

    # 1. The peer may not charge more than the catalog advertised. Charging LESS is
    #    allowed: a price cut between refresh and buy is legitimate and harmless.
    if claimed_price > expected_price_sats:
        raise PeerResponseInvalidError(
            f"peer quoted {claimed_price} sats for a skill the catalog listed at "
            f"{expected_price_sats} sats"
        )

    # 2. Fee-inclusive pricing: the split must reconstruct the price exactly, or an
    #    honest-looking provider invoice can hide an inflated fee invoice.
    if claimed_provider + claimed_fee != claimed_price:
        raise PeerResponseInvalidError(
            f"peer quote does not add up: {claimed_provider} + {claimed_fee} "
            f"!= {claimed_price}"
        )

    if claimed_price == 0:
        return  # free skill: the peer mints no invoices, so there is nothing to check

    # 3. The binding check — what the invoices actually encode.
    _assert_invoice_amount(quote.get("payment_request"), claimed_provider, "provider")
    if claimed_fee > 0:
        _assert_invoice_amount(quote.get("fee_payment_request"), claimed_fee, "fee")


def _assert_invoice_amount(payment_request: str | None, expected_sats: int, label: str) -> None:
    """Assert a bolt11 invoice encodes exactly ``expected_sats``.

    _parse_bolt11_amount returns 0 both for an amountless invoice and for anything it
    cannot parse, so this fails closed on either: an amountless invoice leaves the
    price to the payer's wallet, which is exactly the ambiguity being closed here.
    """
    if not payment_request:
        raise PeerResponseInvalidError(f"peer sent no {label} invoice for a paid skill")
    encoded = _parse_bolt11_amount(payment_request)
    if encoded != expected_sats:
        raise PeerResponseInvalidError(
            f"{label} invoice encodes {encoded} sats but the peer quoted "
            f"{expected_sats} sats"
        )


async def request_remote_execution(
    peer_url: str,
    *,
    skill_id: str,
    expected_price_sats: int,
    consumer_name: str = "anonymous",
    input_data: dict | None = None,
    payer_pubkey: str | None = None,
    timeout: float = _REQUEST_TIMEOUT,
) -> dict:
    """Ask ``peer_url`` to open an execution of ``skill_id``, and verify the quote.

    Returns the peer's payload (its execution id and invoices) once it has been
    checked against ``expected_price_sats`` — the price the cached listing
    advertised. Raises CrossNodeError (never a bare exception) on any refusal.
    """
    payload = {
        "skill_id": skill_id,
        "consumer_name": consumer_name,
        "input_data": input_data,
        "payer_pubkey": payer_pubkey,
    }
    quote = await _post_to_peer(peer_url, _EXECUTIONS_PATH, payload, timeout=timeout)
    _verify_quote(quote, expected_price_sats)
    return quote


async def confirm_remote_execution(
    peer_url: str,
    remote_execution_id: str,
    *,
    payment_hash: str,
    payment_preimage: str,
    timeout: float = _CONFIRM_TIMEOUT,
) -> dict:
    """Relay the consumer's payment proof to the peer and return its result.

    The peer independently verifies that the preimage hashes to the payment hash and
    that its own wallet actually settled the invoice, then runs the provider webhook.
    Nothing here re-implements that; A is a courier for the proof and the output.
    """
    _assert_execution_id(remote_execution_id)
    path = f"{_EXECUTIONS_PATH}/{remote_execution_id}/confirm"
    payload = {"payment_hash": payment_hash, "payment_preimage": payment_preimage}
    return await _post_to_peer(peer_url, path, payload, timeout=timeout)
