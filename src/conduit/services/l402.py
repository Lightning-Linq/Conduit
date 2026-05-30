"""
L402 Protocol — Lightning HTTP 402 authentication for Conduit.

Implements the L402 standard: HTTP 402 Payment Required + macaroon + Lightning
invoice. The protocol flow is:

  1. Client requests a protected endpoint without credentials.
  2. Server returns 402 with a WWW-Authenticate header containing a macaroon
     and a Lightning invoice (BOLT-11).
  3. Client pays the invoice (out of band), obtaining the payment preimage.
  4. Client retries the request with Authorization: L402 <macaroon>:<preimage>.
  5. Server verifies the macaroon signature AND that SHA256(preimage) ==
     payment_hash embedded in the macaroon caveat. Stateless — no DB lookup.

The macaroon cryptographically commits to the payment hash, so the server
can verify payment using only the macaroon + preimage + its root key.

Key design decisions:
  - L402 macaroons are separate from the internal permission macaroons in
    macaroon_auth.py. They use the same pymacaroons library and root secret
    but carry an `l402_payment_hash` caveat instead of permission caveats.
  - An L402 token can optionally embed resource/scope caveats to restrict
    which endpoints or capabilities the token grants access to.
  - Tokens expire with their underlying Lightning invoice (configurable).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from pymacaroons import Macaroon, Verifier

from conduit.core.config import settings


# =============================================================================
# Constants
# =============================================================================

_LOCATION = "conduit-l402"

# Caveat prefixes — keep in sync with _parse helpers below.
_PAYMENT_HASH_PREFIX = "payment_hash = "
_RESOURCE_PREFIX = "resource = "
_EXPIRES_PREFIX = "expires = "

# Regex for parsing the Authorization header:
#   Authorization: L402 <base64_macaroon>:<hex_preimage>
_L402_HEADER_RE = re.compile(
    r"^L402\s+(?P<macaroon>[A-Za-z0-9+/=_-]+):(?P<preimage>[0-9a-fA-F]{64})$"
)


# =============================================================================
# Data types
# =============================================================================

@dataclass
class L402Challenge:
    """Returned to the client on a 402 response."""
    macaroon: str           # base64-serialized macaroon
    invoice: str            # BOLT-11 payment request
    payment_hash: str       # hex — for client convenience
    amount_sats: int        # invoice amount
    expires_at: str | None  # ISO timestamp when the token expires


@dataclass
class L402Credential:
    """Parsed from an incoming Authorization: L402 header."""
    macaroon_raw: str   # base64
    preimage: str       # hex (64 chars = 32 bytes)


@dataclass
class L402VerifyResult:
    """Result of verifying an L402 credential."""
    valid: bool
    payment_hash: str
    resource: str | None    # resource scope, if any
    error: str | None = None


# =============================================================================
# Secret derivation
# =============================================================================

def _get_l402_secret() -> str:
    """
    Get the L402 macaroon root key.

    M10: Uses the dedicated L402_SECRET_KEY setting instead of deriving
    from the API key. This decouples key rotation — rotating the API key
    no longer invalidates outstanding L402 tokens, and a leaked API key
    doesn't compromise L402 token forgery.
    """
    secret = settings.l402_secret_key
    is_placeholder = (
        not secret
        or secret.startswith("CHANGE-ME")
        or secret == "change-me-to-a-random-secret"
    )
    if is_placeholder and settings.is_production:
        raise RuntimeError(
            "L402_SECRET_KEY is not configured. Set a random secret in .env "
            "when L402_ENABLED=true. Generate one with: "
            "python3 -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )
    if is_placeholder:
        # Dev/test fallback — derive from API key like before
        import sys
        print("[l402] WARNING: L402_SECRET_KEY not set, deriving from API key (not safe for production)", file=sys.stderr)
        secret = settings.conduit_api_key + ":l402"
    return hashlib.sha256(secret.encode()).hexdigest()


# =============================================================================
# Token minting
# =============================================================================

def mint_l402_macaroon(
    payment_hash: str,
    *,
    resource: str | None = None,
    expires_at: datetime | None = None,
) -> str:
    """
    Mint an L402 macaroon bound to a Lightning invoice.

    The `payment_hash` caveat ties this token to a specific invoice.
    The client proves payment by presenting the preimage whose SHA-256
    equals this hash.

    Args:
        payment_hash: hex-encoded payment hash from the Lightning invoice.
        resource: optional resource scope (e.g. "marketplace:execute",
                  "skill:<skill_id>") to restrict what this token can access.
        expires_at: optional expiry timestamp. After this time the token
                    is rejected even if the preimage is valid.

    Returns:
        Base64-serialized macaroon string.
    """
    m = Macaroon(
        location=_LOCATION,
        identifier=f"l402-{payment_hash[:16]}",
        key=_get_l402_secret(),
    )

    # Core caveat: binds this token to a specific payment
    m.add_first_party_caveat(f"{_PAYMENT_HASH_PREFIX}{payment_hash}")

    # Optional resource scope
    if resource:
        m.add_first_party_caveat(f"{_RESOURCE_PREFIX}{resource}")

    # Optional expiry
    if expires_at:
        m.add_first_party_caveat(
            f"{_EXPIRES_PREFIX}{int(expires_at.timestamp())}"
        )

    return m.serialize()


def create_l402_challenge(
    lnd,
    *,
    amount_sats: int,
    memo: str = "Conduit L402 access",
    resource: str | None = None,
    expiry_seconds: int | None = None,
) -> L402Challenge:
    """
    Create a full L402 challenge: mint an invoice and a bound macaroon.

    This is the high-level function called by the middleware when a
    request arrives without valid L402 credentials.

    Args:
        lnd: LndClient instance.
        amount_sats: price in satoshis.
        memo: invoice description.
        resource: optional resource scope for the macaroon.
        expiry_seconds: invoice (and token) lifetime in seconds.
                        Defaults to settings.l402_token_expiry_seconds.

    Returns:
        L402Challenge with the macaroon, invoice, and metadata.
    """
    ttl = expiry_seconds or settings.l402_token_expiry_seconds

    # Create the Lightning invoice
    invoice = lnd.create_invoice(
        amount_msats=amount_sats * 1000,
        memo=memo,
        expiry=ttl,
    )

    # Compute expiry timestamp
    expires_at = datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + ttl,
        tz=timezone.utc,
    )

    # Mint the L402 macaroon bound to this invoice's payment hash
    macaroon = mint_l402_macaroon(
        payment_hash=invoice.payment_hash,
        resource=resource,
        expires_at=expires_at,
    )

    return L402Challenge(
        macaroon=macaroon,
        invoice=invoice.payment_request,
        payment_hash=invoice.payment_hash,
        amount_sats=amount_sats,
        expires_at=expires_at.isoformat(),
    )


# =============================================================================
# Credential parsing
# =============================================================================

def parse_l402_header(auth_header: str) -> L402Credential | None:
    """
    Parse an Authorization header into an L402Credential.

    Expected format: Authorization: L402 <base64_macaroon>:<hex_preimage>

    Returns None if the header doesn't match the L402 scheme.
    """
    if not auth_header:
        return None

    match = _L402_HEADER_RE.match(auth_header.strip())
    if not match:
        return None

    return L402Credential(
        macaroon_raw=match.group("macaroon"),
        preimage=match.group("preimage"),
    )


# =============================================================================
# Stateless verification
# =============================================================================

def verify_l402(credential: L402Credential) -> L402VerifyResult:
    """
    Verify an L402 credential: macaroon signature + preimage → payment_hash.

    This is STATELESS — no database or LND lookup. The verification logic:

    1. Deserialize and cryptographically verify the macaroon (checks HMAC
       chain against our root key).
    2. Extract the `payment_hash` caveat from the macaroon.
    3. Compute SHA-256 of the presented preimage.
    4. Confirm SHA-256(preimage) == payment_hash. This proves the caller
       paid the invoice (only the payer receives the preimage).
    5. Check optional `expires` caveat against current time.
    6. Extract optional `resource` caveat for downstream authorization.

    Returns L402VerifyResult with valid=True on success.
    """
    try:
        m = Macaroon.deserialize(credential.macaroon_raw)
    except Exception as e:
        return L402VerifyResult(
            valid=False, payment_hash="", resource=None,
            error=f"Invalid macaroon: {e}",
        )

    # ── Extract caveats ─────────────────────────────────────────────
    payment_hash: str | None = None
    resource: str | None = None
    expires_ts: int | None = None

    for caveat in m.caveats:
        cid = caveat.caveat_id
        if cid.startswith(_PAYMENT_HASH_PREFIX):
            payment_hash = cid[len(_PAYMENT_HASH_PREFIX):]
        elif cid.startswith(_RESOURCE_PREFIX):
            resource = cid[len(_RESOURCE_PREFIX):]
        elif cid.startswith(_EXPIRES_PREFIX):
            try:
                expires_ts = int(cid[len(_EXPIRES_PREFIX):])
            except ValueError:
                return L402VerifyResult(
                    valid=False, payment_hash="", resource=None,
                    error="Malformed expires caveat",
                )
        else:
            return L402VerifyResult(
                valid=False, payment_hash="", resource=None,
                error=f"Unrecognized caveat: {cid[:40]}",
            )

    if not payment_hash:
        return L402VerifyResult(
            valid=False, payment_hash="", resource=None,
            error="Macaroon missing payment_hash caveat",
        )

    # ── Verify macaroon HMAC chain ──────────────────────────────────
    v = Verifier()
    v.satisfy_general(lambda c: c.startswith(_PAYMENT_HASH_PREFIX))
    v.satisfy_general(lambda c: c.startswith(_RESOURCE_PREFIX))
    v.satisfy_general(lambda c: c.startswith(_EXPIRES_PREFIX))

    try:
        v.verify(m, _get_l402_secret())
    except Exception as e:
        return L402VerifyResult(
            valid=False, payment_hash=payment_hash, resource=resource,
            error=f"Macaroon signature invalid: {e}",
        )

    # ── Check expiry ────────────────────────────────────────────────
    if expires_ts is not None:
        now = int(datetime.now(timezone.utc).timestamp())
        if now > expires_ts:
            return L402VerifyResult(
                valid=False, payment_hash=payment_hash, resource=resource,
                error="L402 token has expired",
            )

    # ── Verify preimage → payment_hash ──────────────────────────────
    # This is the core L402 proof: SHA-256(preimage) must equal the
    # payment_hash committed in the macaroon. The preimage is only
    # revealed to the payer when the Lightning invoice settles.
    preimage_bytes = bytes.fromhex(credential.preimage)
    computed_hash = hashlib.sha256(preimage_bytes).hexdigest()

    if computed_hash != payment_hash:
        return L402VerifyResult(
            valid=False, payment_hash=payment_hash, resource=resource,
            error="Preimage does not match payment_hash (payment not proven)",
        )

    return L402VerifyResult(
        valid=True,
        payment_hash=payment_hash,
        resource=resource,
    )


# =============================================================================
# WWW-Authenticate header formatting
# =============================================================================

def format_www_authenticate(challenge: L402Challenge) -> str:
    """
    Format the WWW-Authenticate header value for a 402 response.

    Format per the L402 spec:
      L402 macaroon="<base64>", invoice="<bolt11>"
    """
    return (
        f'L402 macaroon="{challenge.macaroon}", '
        f'invoice="{challenge.invoice}"'
    )
