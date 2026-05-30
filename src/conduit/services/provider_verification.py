"""
Provider verification — proves skill providers control a real Lightning node
or a domain.

Two verification methods:
1. Lightning node proof — provider signs a challenge with their node key.
   Conduit verifies the signature via LND's VerifyMessage RPC, confirming
   the provider controls a real funded node.

2. Domain verification — provider places a challenge token at a well-known
   URL (https://domain/.well-known/conduit-verify.txt) or in a DNS TXT
   record (_conduit-verify.domain). Conduit fetches and confirms.

Verification badges:
- "node_verified"   — proved control of a Lightning node
- "domain_verified" — proved control of a domain
- "fully_verified"  — both methods completed
- "unverified"      — default, no proof submitted
"""

import secrets
import sys
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from conduit.core.config import settings
from conduit.models.skill import Skill
from conduit.services.lnd import lnd_client
from conduit.services.url_safety import UnsafeURLError, validate_domain


# Challenges expire so we don't keep an indefinite outstanding-proof
# window that a provider could collect across many skills.
CHALLENGE_TTL = timedelta(minutes=30)


# =============================================================================
# Challenge generation
# =============================================================================

# Challenge format: "conduit-verify:<random_hex>:<skill_id>:<issued_unix_ts>"
_CHALLENGE_PREFIX = "conduit-verify"


def generate_challenge(skill_id: str) -> str:
    """Generate a unique challenge token for verification."""
    nonce = secrets.token_hex(16)
    issued = int(datetime.now(timezone.utc).timestamp())
    return f"{_CHALLENGE_PREFIX}:{nonce}:{skill_id}:{issued}"


def _challenge_is_fresh(challenge: str) -> bool:
    """True if the challenge was issued within CHALLENGE_TTL."""
    parts = challenge.split(":")
    if len(parts) < 4:
        # Legacy challenge format (pre-TTL). Treat as stale so the
        # provider has to re-request.
        return False
    try:
        issued = int(parts[-1])
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(issued, tz=timezone.utc)
    return age <= CHALLENGE_TTL


# =============================================================================
# Lightning node verification
# =============================================================================


class VerificationError(Exception):
    """Raised when verification fails."""
    pass


async def start_node_verification(
    session: AsyncSession,
    skill_id: str,
) -> str:
    """
    Start Lightning node verification for a skill.

    Generates a challenge message the provider must sign with their
    node's private key (via `lncli signmessage`).

    Returns the challenge string.
    """
    skill = await _get_skill(session, skill_id)

    # H7: Reject if there's already a fresh (unexpired) challenge pending.
    # This prevents an attacker from overwriting a legitimate provider's
    # challenge by calling request_verification after them.
    if skill.verification_challenge and _challenge_is_fresh(skill.verification_challenge):
        raise VerificationError(
            "This skill already has a pending verification challenge. "
            "Wait for it to expire or submit the existing challenge."
        )

    # Generate and store challenge
    challenge = generate_challenge(skill_id)
    skill.verification_challenge = challenge
    await session.commit()

    print(f"[verification] Node challenge generated for skill {skill_id}", file=sys.stderr)
    return challenge


async def verify_node_signature(
    session: AsyncSession,
    skill_id: str,
    signature: str,
    lnd: "LndClient | None" = None,
) -> dict:
    """
    Complete Lightning node verification by verifying the signed challenge.

    The provider signs the challenge with `lncli signmessage "<challenge>"`,
    then submits the signature here. We verify it via LND's VerifyMessage
    and extract the signer's pubkey.
    """
    skill = await _get_skill(session, skill_id)

    if not skill.verification_challenge:
        raise VerificationError(
            "No active verification challenge for this skill. "
            "Call request_verification first."
        )

    challenge = skill.verification_challenge

    if not _challenge_is_fresh(challenge):
        # Drop stale challenge so the provider must request a new one.
        skill.verification_challenge = None
        await session.commit()
        raise VerificationError(
            "Verification challenge has expired. Call request_verification again."
        )

    # Verify signature via LND
    client = lnd or lnd_client
    try:
        result = client.verify_message(challenge, signature)
    except Exception as e:
        raise VerificationError(f"Signature verification failed: {e}")

    if not result["valid"]:
        raise VerificationError("Invalid signature — does not match the challenge.")

    signer_pubkey = result["pubkey"]

    # If the skill claimed a specific provider_pubkey when registering,
    # the signer MUST match it. Otherwise the badge proves "I control some
    # node," not "I control the node this skill claims."
    claimed = skill.provider_pubkey  # L7: direct access, not getattr
    if claimed and claimed.strip() and claimed != signer_pubkey:
        raise VerificationError(
            "Signature is valid but the signing node "
            f"({signer_pubkey[:16]}...) does not match the pubkey "
            f"this skill was registered under ({claimed[:16]}...)."
        )

    # Update skill with verified pubkey
    skill.verified_node_pubkey = signer_pubkey
    skill.verification_challenge = None  # clear challenge

    # Update verification status
    if skill.verified_domain:
        skill.verification_status = "fully_verified"
    else:
        skill.verification_status = "node_verified"
    skill.verified_at = datetime.now(timezone.utc)

    await session.commit()

    print(
        f"[verification] Node verified for skill {skill_id}: pubkey={signer_pubkey[:16]}...",
        file=sys.stderr,
    )

    return {
        "status": skill.verification_status,
        "pubkey": signer_pubkey,
        "skill_name": skill.name,
        "provider": skill.provider_name,
    }


# =============================================================================
# Domain verification
# =============================================================================


async def start_domain_verification(
    session: AsyncSession,
    skill_id: str,
    domain: str,
) -> str:
    """
    Start domain verification for a skill.

    Generates a challenge token the provider must place at either:
    - https://{domain}/.well-known/conduit-verify.txt  (file contents = challenge)
    - DNS TXT record: _conduit-verify.{domain}  (record value = challenge)

    Both methods are checked during verification — provider only needs one.

    Returns the challenge token.
    """
    skill = await _get_skill(session, skill_id)

    # Reject anything that doesn't look like a public hostname up-front so
    # the operator can't be tricked into generating a challenge for
    # "localhost" or an RFC1918 address.
    try:
        validate_domain(domain)
    except UnsafeURLError as e:
        raise VerificationError(f"Refusing to verify domain {domain!r}: {e}")

    # H7: Reject if there's already a fresh challenge pending.
    if skill.verification_challenge and _challenge_is_fresh(skill.verification_challenge):
        raise VerificationError(
            "This skill already has a pending verification challenge. "
            "Wait for it to expire or submit the existing challenge."
        )

    # Generate and store challenge
    challenge = generate_challenge(skill_id)
    skill.verification_challenge = challenge
    await session.commit()

    print(f"[verification] Domain challenge generated for {domain}", file=sys.stderr)
    return challenge


async def verify_domain(
    session: AsyncSession,
    skill_id: str,
    domain: str,
) -> dict:
    """
    Complete domain verification by checking the well-known URL.

    Fetches https://{domain}/.well-known/conduit-verify.txt and checks
    if it contains the challenge token.
    """
    skill = await _get_skill(session, skill_id)

    # Re-validate domain on the submit side too: skills registered before
    # this fix may have a hostile domain stored, and we never want to fetch
    # internal URLs even if the request slipped through earlier.
    try:
        domain = validate_domain(domain)
    except UnsafeURLError as e:
        raise VerificationError(f"Refusing to verify domain {domain!r}: {e}")

    if not skill.verification_challenge:
        raise VerificationError(
            "No active verification challenge for this skill. "
            "Call request_verification first."
        )

    challenge = skill.verification_challenge

    if not _challenge_is_fresh(challenge):
        skill.verification_challenge = None
        await session.commit()
        raise VerificationError(
            "Verification challenge has expired. Call request_verification again."
        )

    # Try well-known URL
    well_known_url = f"https://{domain}/.well-known/conduit-verify.txt"
    verified = False

    try:
        async with httpx.AsyncClient(
            timeout=10.0, follow_redirects=False
        ) as client:
            response = await client.get(well_known_url)
            if response.status_code == 200:
                # Exact-match (after strip) — substring match lets any page
                # that mirrors user content (paste, gist, status banner)
                # "contain" the challenge and pass.
                if response.text.strip() == challenge:
                    verified = True
    except httpx.RequestError as e:
        print(f"[verification] Well-known fetch failed for {domain}: {e}", file=sys.stderr)

    # Fall back to DNS TXT record: _conduit-verify.{domain}
    if not verified:
        verified = await _check_dns_txt(domain, challenge)

    if not verified:
        raise VerificationError(
            f"Challenge not found. Tried:\n"
            f"  1. {well_known_url} (file must contain only the challenge)\n"
            f"  2. DNS TXT record at _conduit-verify.{domain}\n"
            f"Challenge value:\n{challenge}"
        )

    # Update skill
    skill.verified_domain = domain
    skill.verification_challenge = None

    if skill.verified_node_pubkey:
        skill.verification_status = "fully_verified"
    else:
        skill.verification_status = "domain_verified"
    skill.verified_at = datetime.now(timezone.utc)

    await session.commit()

    print(f"[verification] Domain verified for skill {skill_id}: {domain}", file=sys.stderr)

    return {
        "status": skill.verification_status,
        "domain": domain,
        "skill_name": skill.name,
        "provider": skill.provider_name,
    }


# =============================================================================
# Status
# =============================================================================


async def get_verification_status(
    session: AsyncSession,
    skill_id: str,
) -> dict:
    """Get the current verification status for a skill.

    Checks for expiry on every status call — if the badge has expired,
    it is downgraded before returning.
    """
    skill = await _get_skill(session, skill_id)

    # Check and enforce expiry
    await enforce_expiry(session, skill)

    return {
        "skill_id": str(skill.id),
        "skill_name": skill.name,
        "provider": skill.provider_name,
        "verification_status": skill.verification_status,
        "verified_node_pubkey": skill.verified_node_pubkey,
        "verified_domain": skill.verified_domain,
        "verified_at": skill.verified_at.isoformat() if skill.verified_at else None,
        "has_pending_challenge": skill.verification_challenge is not None,
    }


# =============================================================================
# DNS TXT verification
# =============================================================================


async def _check_dns_txt(domain: str, expected_challenge: str) -> bool:
    """
    Check for a DNS TXT record at _conduit-verify.{domain}.

    The TXT record value must exactly match the challenge string.
    Uses the system resolver via asyncio.
    """
    import asyncio
    import socket

    lookup_name = f"_conduit-verify.{domain}"
    try:
        # H8: Run blocking DNS lookup in a thread to avoid stalling the event loop.
        import dns.resolver

        def _resolve():
            resolver = dns.resolver.Resolver()
            resolver.lifetime = 5.0  # 5s timeout
            return resolver.resolve(lookup_name, "TXT")

        answers = await asyncio.to_thread(_resolve)
        for rdata in answers:
            # TXT records come as a list of byte strings
            txt_value = b"".join(rdata.strings).decode("utf-8").strip()
            if txt_value == expected_challenge:
                print(
                    f"[verification] DNS TXT match for {lookup_name}",
                    file=sys.stderr,
                )
                return True
    except ImportError:
        # dnspython not installed — skip DNS verification silently.
        # The well-known URL path is always available.
        print(
            "[verification] dnspython not installed — DNS TXT verification unavailable",
            file=sys.stderr,
        )
    except Exception as e:
        print(
            f"[verification] DNS TXT lookup failed for {lookup_name}: {e}",
            file=sys.stderr,
        )

    return False


# =============================================================================
# Verification expiry
# =============================================================================


def check_verification_expiry(skill: Skill) -> bool:
    """
    Check if a skill's verification has expired.

    Returns True if expired (badge should be downgraded), False if still valid.
    A skill with no verified_at or verification_expiry_days=0 never expires.
    """

    if settings.verification_expiry_days == 0:
        return False  # expiry disabled

    if not skill.verified_at:
        return False  # never verified

    if skill.verification_status == "unverified":
        return False  # nothing to expire

    age = datetime.now(timezone.utc) - skill.verified_at
    return age > timedelta(days=settings.verification_expiry_days)


async def enforce_expiry(session: AsyncSession, skill: Skill) -> bool:
    """
    If the skill's verification has expired, downgrade to 'unverified'
    and clear the badges. Returns True if the badge was expired.
    """
    if not check_verification_expiry(skill):
        return False

    old_status = skill.verification_status
    skill.verification_status = "expired"
    skill.verified_node_pubkey = None
    skill.verified_domain = None
    skill.verified_at = None
    await session.commit()

    print(
        f"[verification] Badge expired for skill {skill.id}: "
        f"{old_status} → expired",
        file=sys.stderr,
    )
    return True


# =============================================================================
# Helpers
# =============================================================================


async def _get_skill(session: AsyncSession, skill_id: str) -> Skill:
    """Fetch a skill by ID or raise VerificationError."""
    import uuid
    try:
        uid = uuid.UUID(skill_id)
    except ValueError:
        raise VerificationError(f"Invalid skill ID: {skill_id}")

    result = await session.execute(
        select(Skill).where(Skill.id == uid)
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise VerificationError(f"Skill not found: {skill_id}")

    return skill
