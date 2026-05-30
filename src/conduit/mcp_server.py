"""
Conduit Lightning MCP Server

Exposes Lightning Network capabilities, a skill marketplace, and
decentralized Nostr discovery to AI agents via the Model Context Protocol.

Lightning Tools (6):
- create/pay/decode invoices, check payments, get balance and node info

Marketplace Tools (7):
- register, discover, and execute skills
- submit ratings backed by Lightning payment proofs

Nostr Tools (4):
- publish skills to Nostr relays for decentralized discovery
- discover skills across relays, check relay status, view identity

Security Tools (6):
- spending limits, macaroon auth, anomaly detection, provider verification

L402 Tools (3):
- create/verify L402 tokens, check L402 configuration status

Non-custodial design: payments flow directly between agents on Lightning.
Conduit provides coordination, discovery, and reputation — never custody.

Usage:
    python -m conduit.mcp_server
"""

import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent
from sqlalchemy import select, func as sa_func, or_
from sqlalchemy.ext.asyncio import AsyncSession

# Ensure proto_generated is importable
_proto_path = Path(__file__).parent / "services" / "proto_generated"
if str(_proto_path) not in sys.path:
    sys.path.insert(0, str(_proto_path))

from conduit.services.lnd import LndClient
from conduit.services.spending_limiter import (
    cancel_reservation,
    check_spending_limits,
    record_successful_payment,
    get_spending_summary,
    SpendingLimitExceeded,
    ConfirmationRequired,
)
from conduit.services.skill_executor import execute_skill_webhook, SkillExecutionError
from conduit.services.anomaly_detector import check_for_anomalies, get_anomaly_summary
from conduit.services.fee_calculator import calculate_fee
from conduit.services.rating_integrity import (
    validate_rating,
    check_provider_rating_concentration,
    calculate_weighted_rating,
    RatingIntegrityError,
)
from conduit.services.rate_limiter import rate_limiter, RateLimitExceeded
from conduit.services.provider_verification import (
    start_node_verification,
    start_domain_verification,
    verify_node_signature,
    verify_domain,
    get_verification_status,
    VerificationError,
)
from conduit.services.macaroon_auth import (
    check_tool_permission,
    initialize_root_session,
    derive_macaroon,
    set_active_macaroon,
    get_active_permissions,
    PROFILES,
    TOOL_PERMISSIONS,
    Permission,
)
from conduit.services.nostr import (
    NostrKeypair,
    NostrEvent,
    NostrRelay,
    skill_to_event,
    event_to_skill,
    publish_to_relays,
    discover_from_relays,
    SKILL_EVENT_KIND,
)
from conduit.core.database import async_session_factory
from conduit.models.skill import Skill
from conduit.models.execution import SkillExecution, ExecutionStatus
from conduit.models.rating import Rating

# Initialize MCP server
server = Server("conduit-lightning")

# LND client instance (connects on first use)
_lnd: LndClient | None = None
# Nostr keypair (loaded on first use)
_nostr_keys: NostrKeypair | None = None


def get_nostr_keys() -> NostrKeypair:
    """Get or create the Nostr keypair for this node."""
    global _nostr_keys
    if _nostr_keys is None:
        from conduit.core.config import settings
        if settings.nostr_private_key:
            key = settings.nostr_private_key
            if key.startswith("nsec"):
                _nostr_keys = NostrKeypair.from_nsec(key)
            else:
                _nostr_keys = NostrKeypair.from_hex(key)
            print(f"[nostr] Loaded key: {_nostr_keys.npub[:20]}...", file=sys.stderr)
        else:
            _nostr_keys = NostrKeypair.generate()
            print(
                f"[nostr] Generated new keypair: {_nostr_keys.npub}\n"
                f"[nostr] Save this nsec to persist identity: {_nostr_keys.nsec}",
                file=sys.stderr,
            )
    return _nostr_keys


def get_nostr_relays(override: list[str] | None = None) -> list[str]:
    """Get relay URLs from override or config."""
    if override:
        return override
    from conduit.core.config import settings
    return settings.nostr_relay_list


def get_lnd() -> LndClient:
    """Get or create the LND client connection."""
    global _lnd
    if _lnd is None or not _lnd.is_connected:
        _lnd = LndClient()
        _lnd.connect()
    return _lnd


# =============================================================================
# Database Helpers
# =============================================================================


async def get_session() -> AsyncSession:
    """Create a new async database session."""
    return async_session_factory()


async def _seed_demo_skills_if_empty():
    """Seed demo skills into Postgres if the skills table is empty."""
    async with async_session_factory() as session:
        result = await session.execute(select(sa_func.count(Skill.id)))
        count = result.scalar()
        if count > 0:
            return  # Already have skills, don't re-seed

        demos = [
            Skill(
                name="English to Spanish Translation",
                description="Translates English text to Spanish using a fine-tuned LLM. Returns translated text.",
                category="translation",
                tags="language,spanish,translation,nlp",
                price_sats=50,
                provider_name="LangBot",
                provider_lightning_address="langbot@getalby.com",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
                output_schema={"type": "object", "properties": {"translated_text": {"type": "string"}}},
                total_executions=0,
                avg_rating=0.0,
                is_active=True,
            ),
            Skill(
                name="Bitcoin Price Analysis",
                description="Analyzes current BTC price action, on-chain metrics, and returns a summary with key levels.",
                category="analytics",
                tags="bitcoin,price,analysis,onchain",
                price_sats=100,
                provider_name="ChainSight",
                provider_lightning_address="chainsight@getalby.com",
                input_schema={"type": "object", "properties": {"timeframe": {"type": "string", "enum": ["1h", "4h", "1d", "1w"]}}, "required": ["timeframe"]},
                output_schema={"type": "object", "properties": {"summary": {"type": "string"}, "key_levels": {"type": "array"}}},
                total_executions=0,
                avg_rating=0.0,
                is_active=True,
            ),
            Skill(
                name="Lightning Channel Advisor",
                description="Analyzes your node's channel graph and recommends optimal peers to open channels with.",
                category="lightning",
                tags="lightning,channels,routing,optimization",
                price_sats=200,
                provider_name="NodeWhisperer",
                provider_lightning_address="nodewhisperer@getalby.com",
                input_schema={"type": "object", "properties": {"node_pubkey": {"type": "string"}}, "required": ["node_pubkey"]},
                output_schema={"type": "object", "properties": {"recommendations": {"type": "array"}, "analysis": {"type": "string"}}},
                total_executions=0,
                avg_rating=0.0,
                is_active=True,
            ),
        ]
        session.add_all(demos)
        await session.commit()


# Flag to track if we've seeded on this run
_db_seeded = False


# =============================================================================
# Tool Definitions
# =============================================================================


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return all available tools — Lightning + Marketplace."""
    return [
        # --- Lightning Tools ---
        Tool(
            name="get_node_info",
            description=(
                "Get information about the connected Lightning node: "
                "alias, pubkey, number of channels, peers, sync status, and version."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="get_balance",
            description=(
                "Get the current balance of the Lightning node. "
                "Returns channel balance (spendable via Lightning), "
                "pending channel balance, and on-chain balance in satoshis."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="create_invoice",
            description=(
                "Create a Lightning invoice (BOLT-11) to receive a payment. "
                "Returns the payment request string that a payer can use to send sats."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "amount_sats": {
                        "type": "integer",
                        "description": "Amount in satoshis to request",
                    },
                    "memo": {
                        "type": "string",
                        "description": "Description attached to the invoice",
                        "default": "",
                    },
                    "expiry_seconds": {
                        "type": "integer",
                        "description": "Seconds until expiry (default: 3600)",
                        "default": 3600,
                    },
                },
                "required": ["amount_sats"],
            },
        ),
        Tool(
            name="pay_invoice",
            description=(
                "Pay a Lightning invoice (BOLT-11 payment request). "
                "Sends satoshis from the connected node to the invoice destination. "
                "Subject to spending limits. If the amount exceeds the confirmation "
                "threshold, this call returns a one-shot confirmation_token. You "
                "MUST surface it to the user, get explicit approval, and only then "
                "call pay_invoice again passing the token back."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "payment_request": {
                        "type": "string",
                        "description": "The BOLT-11 invoice string to pay",
                    },
                    "max_fee_sats": {
                        "type": "integer",
                        "description": "Maximum routing fee in sats (default: 10)",
                        "default": 10,
                    },
                    "confirmation_token": {
                        "type": "string",
                        "description": (
                            "One-shot token returned by a prior pay_invoice call "
                            "when the amount exceeded the confirmation threshold. "
                            "Server-issued; bound to this exact (tool, amount, "
                            "payment_hash). Expires in ~2 minutes."
                        ),
                    },
                },
                "required": ["payment_request"],
            },
        ),
        Tool(
            name="decode_invoice",
            description=(
                "Decode a Lightning invoice (BOLT-11) without paying it. "
                "Returns destination, amount, description, and expiry."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "payment_request": {
                        "type": "string",
                        "description": "The BOLT-11 invoice to decode",
                    },
                },
                "required": ["payment_request"],
            },
        ),
        Tool(
            name="check_payment",
            description=(
                "Check the status of a payment or invoice by payment hash."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "payment_hash": {
                        "type": "string",
                        "description": "The hex-encoded payment hash",
                    },
                },
                "required": ["payment_hash"],
            },
        ),

        # --- Spending / Security Tools ---
        Tool(
            name="get_spending_status",
            description=(
                "Check current spending limits and how much has been spent. "
                "Shows per-payment max, hourly/daily totals, and remaining budget."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="create_macaroon",
            description=(
                "Create a scoped authorization token (macaroon) for an agent. "
                "Use profiles: 'admin' (full access), 'readonly' (no payments/writes), "
                "'marketplace' (skills only, no Lightning), 'spending' (Lightning + skills, no registration). "
                "Or specify custom permissions."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "Permission profile: 'admin', 'readonly', 'marketplace', or 'spending'",
                    },
                    "permissions": {
                        "type": "array",
                        "description": "Custom list of permissions (alternative to profile)",
                        "items": {"type": "string"},
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="list_permissions",
            description=(
                "Show the current session's active permissions and "
                "which tools each permission scope grants access to."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        Tool(
            name="get_anomaly_report",
            description=(
                "View flagged suspicious transaction patterns. Shows summary of "
                "anomaly flags by type and severity, plus the most recent flags."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        # --- Marketplace Tools ---
        Tool(
            name="discover_skills",
            description=(
                "Search the Conduit skill marketplace. "
                "Find skills by keyword, category, or price range. "
                "Returns a list of available skills with pricing and provider info."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keywords (matches name, description, tags)",
                        "default": "",
                    },
                    "category": {
                        "type": "string",
                        "description": "Filter by category (e.g. 'translation', 'analytics', 'lightning')",
                        "default": "",
                    },
                    "max_price_sats": {
                        "type": "integer",
                        "description": "Maximum price in sats (0 = no limit)",
                        "default": 0,
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="get_skill_details",
            description=(
                "Get full details about a specific skill including pricing, "
                "input/output schemas, provider info, and ratings."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The UUID of the skill to look up",
                    },
                },
                "required": ["skill_id"],
            },
        ),
        Tool(
            name="register_skill",
            description=(
                "Register a new skill on the Conduit marketplace. "
                "Provide your Lightning address so consumers pay you directly."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name",
                    },
                    "description": {
                        "type": "string",
                        "description": "What this skill does",
                    },
                    "category": {
                        "type": "string",
                        "description": "Category (e.g. 'translation', 'analytics', 'code', 'data')",
                    },
                    "price_sats": {
                        "type": "number",
                        "description": "Price per execution in satoshis",
                    },
                    "provider_name": {
                        "type": "string",
                        "description": "Your agent/provider name",
                    },
                    "provider_lightning_address": {
                        "type": "string",
                        "description": "Lightning address for receiving payments (e.g. 'you@getalby.com')",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags for discovery",
                        "default": "",
                    },
                    "input_schema": {
                        "type": "object",
                        "description": "JSON Schema describing required input",
                        "default": {},
                    },
                    "output_schema": {
                        "type": "object",
                        "description": "JSON Schema describing the output",
                        "default": {},
                    },
                },
                "required": ["name", "description", "category", "price_sats", "provider_name", "provider_lightning_address"],
            },
        ),
        Tool(
            name="request_skill_execution",
            description=(
                "Request to execute a skill. Returns the provider's Lightning "
                "invoice to pay. After paying, call confirm_skill_execution "
                "with the payment preimage to trigger execution."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The skill to execute",
                    },
                    "input_data": {
                        "type": "object",
                        "description": "Input data matching the skill's input schema",
                        "default": {},
                    },
                    "consumer_name": {
                        "type": "string",
                        "description": "Your agent name (for reputation tracking)",
                        "default": "anonymous",
                    },
                },
                "required": ["skill_id"],
            },
        ),
        Tool(
            name="confirm_skill_execution",
            description=(
                "Confirm payment and trigger skill execution. After paying the "
                "invoice from request_skill_execution, call this with the execution ID "
                "and payment preimage. Conduit verifies settlement, calls the provider's "
                "webhook, and returns the skill output."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "execution_id": {
                        "type": "string",
                        "description": "The execution ID from request_skill_execution",
                    },
                    "payment_preimage": {
                        "type": "string",
                        "description": "The payment preimage (proof of payment) from pay_invoice",
                    },
                },
                "required": ["execution_id", "payment_preimage"],
            },
        ),
        Tool(
            name="submit_rating",
            description=(
                "Rate a skill execution. Requires the payment preimage as proof "
                "that you actually used and paid for the skill — no fake reviews."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "execution_id": {
                        "type": "string",
                        "description": "The execution to rate",
                    },
                    "score": {
                        "type": "integer",
                        "description": "Rating from 1 (poor) to 5 (excellent)",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Optional review comment",
                        "default": "",
                    },
                    "payment_preimage": {
                        "type": "string",
                        "description": "Payment preimage as proof of purchase",
                    },
                },
                "required": ["execution_id", "score", "payment_preimage"],
            },
        ),

        # --- Verification Tools ---
        Tool(
            name="request_verification",
            description=(
                "Start provider verification for a skill. Choose 'node' to prove "
                "you control a Lightning node (sign a challenge with lncli), or "
                "'domain' to prove you control a domain (host a challenge file). "
                "Returns the challenge to complete."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The skill to verify",
                    },
                    "method": {
                        "type": "string",
                        "description": "Verification method: 'node' or 'domain'",
                        "enum": ["node", "domain"],
                    },
                    "domain": {
                        "type": "string",
                        "description": "Domain to verify (required if method is 'domain')",
                        "default": "",
                    },
                },
                "required": ["skill_id", "method"],
            },
        ),
        Tool(
            name="submit_verification",
            description=(
                "Complete a pending verification. For 'node' method, submit the "
                "signature from `lncli signmessage`. For 'domain' method, call "
                "this after placing the challenge at the well-known URL."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The skill being verified",
                    },
                    "method": {
                        "type": "string",
                        "description": "Verification method: 'node' or 'domain'",
                        "enum": ["node", "domain"],
                    },
                    "signature": {
                        "type": "string",
                        "description": "Node signature (required for 'node' method)",
                        "default": "",
                    },
                    "domain": {
                        "type": "string",
                        "description": "Domain to check (required for 'domain' method)",
                        "default": "",
                    },
                },
                "required": ["skill_id", "method"],
            },
        ),
        Tool(
            name="get_verification_status",
            description=(
                "Check the verification status of a skill. Shows whether the "
                "provider has been verified via Lightning node proof, domain "
                "proof, both, or neither."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The skill to check",
                    },
                },
                "required": ["skill_id"],
            },
        ),

        # --- Nostr Tools ---
        Tool(
            name="nostr_publish_skill",
            description=(
                "Publish a skill listing to Nostr relays for decentralized discovery. "
                "Any agent on any relay can find your skill without depending on "
                "Conduit's centralized marketplace. Publishes as a replaceable event "
                "(kind 38383) so re-publishing updates the listing."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "skill_id": {
                        "type": "string",
                        "description": "The skill ID to publish (must exist in local marketplace)",
                    },
                    "relays": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relay URLs to publish to (default: configured relays)",
                        "default": [],
                    },
                },
                "required": ["skill_id"],
            },
        ),
        Tool(
            name="nostr_discover_skills",
            description=(
                "Discover Conduit skills published on Nostr relays. "
                "Search across decentralized relays by category or keyword. "
                "Returns skills from any provider on any relay."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Filter by category (e.g. 'analytics', 'translation')",
                        "default": "",
                    },
                    "max_price_sats": {
                        "type": "integer",
                        "description": "Maximum price in sats (0 = no limit)",
                        "default": 0,
                    },
                    "relays": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Relay URLs to search (default: configured relays)",
                        "default": [],
                    },
                },
                "required": [],
            },
        ),
        Tool(
            name="nostr_get_profile",
            description=(
                "Get the Nostr identity for this Conduit node. Shows the "
                "public key (npub), configured relays, and how many skills "
                "have been published to Nostr."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="nostr_relay_status",
            description=(
                "Check connectivity to configured Nostr relays. "
                "Tests each relay and reports which ones are reachable."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),

        # --- L402 Tools ---
        Tool(
            name="create_l402_token",
            description=(
                "Create an L402 access token: mints a Lightning invoice and a "
                "bound macaroon. The caller pays the invoice (receiving the "
                "preimage), then presents Authorization: L402 <macaroon>:<preimage> "
                "to access protected endpoints. Stateless verification — no DB "
                "lookup needed. Use this to gate any external API or resource "
                "behind a Lightning paywall."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "amount_sats": {
                        "type": "integer",
                        "description": "Price in satoshis for the token",
                    },
                    "memo": {
                        "type": "string",
                        "description": "Description for the Lightning invoice",
                        "default": "Conduit L402 access",
                    },
                    "resource": {
                        "type": "string",
                        "description": (
                            "Optional resource scope to restrict this token "
                            "(e.g. 'marketplace', 'lightning', 'skill:<id>')"
                        ),
                    },
                    "expiry_seconds": {
                        "type": "integer",
                        "description": "Token lifetime in seconds (default: from config)",
                    },
                },
                "required": ["amount_sats"],
            },
        ),
        Tool(
            name="verify_l402_token",
            description=(
                "Verify an L402 credential (macaroon + preimage). Stateless "
                "verification: checks macaroon HMAC chain, confirms "
                "SHA256(preimage) == payment_hash, and validates expiry. "
                "No LND or database call required."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "macaroon": {
                        "type": "string",
                        "description": "Base64-serialized L402 macaroon",
                    },
                    "preimage": {
                        "type": "string",
                        "description": "Hex-encoded payment preimage (64 chars)",
                    },
                },
                "required": ["macaroon", "preimage"],
            },
        ),
        Tool(
            name="get_l402_status",
            description=(
                "Get the current L402 configuration: whether it's enabled, "
                "default pricing, token expiry, and which routes are free."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
    ]


# =============================================================================
# Tool Implementations
# =============================================================================


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    """Execute a tool and return the result."""
    try:
        # Ensure demo skills exist in database on first tool call
        global _db_seeded
        if not _db_seeded:
            await _seed_demo_skills_if_empty()
            _db_seeded = True

        # --- Permission check (macaroon scoping) ---
        try:
            check_tool_permission(name)
        except PermissionError as e:
            return [TextContent(type="text", text=f"ACCESS DENIED: {e}")]

        # --- Rate limiting (MCP uses the configured API key as client ID) ---
        try:
            rate_limiter.check(name, client_id="mcp")
        except RateLimitExceeded as e:
            return [TextContent(type="text", text=f"RATE LIMITED: {e}")]

        # --- Lightning Tools ---
        if name in ("get_node_info", "get_balance", "create_invoice",
                     "pay_invoice", "decode_invoice", "check_payment"):
            return await _handle_lightning_tool(name, arguments)

        # --- Spending / Security Tools ---
        elif name == "get_spending_status":
            return await _get_spending_status()
        elif name == "create_macaroon":
            return _create_macaroon(arguments)
        elif name == "list_permissions":
            return _list_permissions()
        elif name == "get_anomaly_report":
            return await _get_anomaly_report()

        # --- Marketplace Tools ---
        elif name == "discover_skills":
            return await _discover_skills(arguments)
        elif name == "get_skill_details":
            return await _get_skill_details(arguments)
        elif name == "register_skill":
            return await _register_skill(arguments)
        elif name == "request_skill_execution":
            return await _request_skill_execution(arguments)
        elif name == "confirm_skill_execution":
            return await _confirm_skill_execution(arguments)
        elif name == "submit_rating":
            return await _submit_rating(arguments)

        # --- Verification Tools ---
        elif name == "request_verification":
            return await _request_verification(arguments)
        elif name == "submit_verification":
            return await _submit_verification(arguments)
        elif name == "get_verification_status":
            return await _get_verification_status(arguments)

        # --- Nostr Tools ---
        elif name == "nostr_publish_skill":
            return await _nostr_publish_skill(arguments)
        elif name == "nostr_discover_skills":
            return await _nostr_discover_skills(arguments)
        elif name == "nostr_get_profile":
            return await _nostr_get_profile()
        elif name == "nostr_relay_status":
            return await _nostr_relay_status(arguments)

        # --- L402 Tools ---
        elif name == "create_l402_token":
            return await _create_l402_token(arguments)
        elif name == "verify_l402_token":
            return await _verify_l402_token(arguments)
        elif name == "get_l402_status":
            return await _get_l402_status()
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(f"[call_tool] Error in {name}: {tb}", file=sys.stderr)
        return [TextContent(type="text", text=f"Error in {name}: {type(e).__name__}: {str(e)}")]


async def _handle_lightning_tool(name: str, arguments: dict) -> list[TextContent]:
    """Handle Lightning Network tools."""
    lnd = get_lnd()

    if name == "get_node_info":
        info = lnd.get_info()
        return [TextContent(
            type="text",
            text=(
                f"Node Alias: {info.alias}\n"
                f"Pubkey: {info.pubkey}\n"
                f"Active Channels: {info.num_active_channels}\n"
                f"Peers: {info.num_peers}\n"
                f"Block Height: {info.block_height}\n"
                f"Synced to Chain: {info.synced_to_chain}\n"
                f"Version: {info.version}"
            ),
        )]

    elif name == "get_balance":
        bal = lnd.get_balance()
        return [TextContent(
            type="text",
            text=(
                f"Channel Balance: {bal['channel_balance_sats']:,} sats (spendable via Lightning)\n"
                f"Pending Channels: {bal['channel_pending_sats']:,} sats\n"
                f"On-chain Confirmed: {bal['onchain_confirmed_sats']:,} sats\n"
                f"On-chain Unconfirmed: {bal['onchain_unconfirmed_sats']:,} sats\n"
                f"On-chain Total: {bal['onchain_total_sats']:,} sats"
            ),
        )]

    elif name == "create_invoice":
        amount_sats = int(arguments["amount_sats"])
        memo = str(arguments.get("memo", ""))
        expiry = int(arguments.get("expiry_seconds", 3600))
        invoice = lnd.create_invoice(
            amount_msats=amount_sats * 1000, memo=memo, expiry=expiry,
        )

        # Generate QR code PNG file for the payment request
        qr_note = ""
        try:
            import qrcode
            import qrcode.constants

            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=12,
                border=5,
            )
            qr.add_data(f"lightning:{invoice.payment_request.upper()}")
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")

            qr_dir = Path.home() / "Desktop" / "Claude" / "conduit-qr"
            qr_dir.mkdir(parents=True, exist_ok=True)
            qr_file = qr_dir / f"invoice-{invoice.payment_hash[:12]}.png"
            img.save(str(qr_file), format="PNG")
            qr_note = f"\nQR Code: {qr_file}"
            print(f"[create_invoice] QR saved to {qr_file}", file=sys.stderr)
        except Exception as e:
            print(f"[create_invoice] QR generation failed: {e}", file=sys.stderr)

        return [TextContent(
            type="text",
            text=(
                f"Invoice Created!\n"
                f"Amount: {amount_sats:,} sats\n"
                f"Payment Hash: {invoice.payment_hash}\n"
                f"Payment Request: {invoice.payment_request}"
                f"{qr_note}\n"
                f"\nShare the Payment Request or open the QR code to scan."
            ),
        )]

    elif name == "pay_invoice":
        payment_request = arguments["payment_request"]
        max_fee_sats = arguments.get("max_fee_sats", 10)
        confirmation_token = arguments.get("confirmation_token")

        # Decode first to get amount for limit check
        decoded = lnd.decode_invoice(payment_request)
        amount_sats = decoded["amount_sats"]
        description = decoded.get("description", "") or "Lightning payment"
        invoice_payment_hash = decoded.get("payment_hash") or ""

        # Check spending limits. The confirmation_token, if supplied, is
        # bound to (tool, amount, payment_hash) — an agent can't simply
        # flip a boolean to bypass the prompt for large payments.
        reservation_id = None
        try:
            reservation_id = await check_spending_limits(
                amount_sats=amount_sats,
                tool_name="pay_invoice",
                description=description,
                confirmation_token=confirmation_token,
                payment_hash=invoice_payment_hash,
            )
        except SpendingLimitExceeded as e:
            return [TextContent(
                type="text",
                text=f"⚠️ PAYMENT BLOCKED\n{e.reason}\n\nAdjust limits in .env or wait for the window to reset.",
            )]
        except ConfirmationRequired as e:
            return [TextContent(
                type="text",
                text=(
                    f"⚠️ CONFIRMATION REQUIRED\n"
                    f"Payment of {e.amount_sats:,} sats exceeds confirmation threshold "
                    f"of {e.threshold_sats:,} sats.\n"
                    f"Description: {e.description}\n\n"
                    f"To proceed, ASK THE USER to approve, then call pay_invoice\n"
                    f"again with this token (expires in {e.expires_in_seconds}s):\n"
                    f"  confirmation_token={e.confirmation_token}"
                ),
            )]

        # Limits passed — execute payment
        result = lnd.pay_invoice(
            payment_request=payment_request,
            max_fee_msats=max_fee_sats * 1000,
        )
        if result.status == "SUCCEEDED":
            # Post-payment bookkeeping (non-critical — payment already sent)
            anomaly_note = ""
            try:
                await record_successful_payment(
                    amount_sats=amount_sats,
                    tool_name="pay_invoice",
                    description=description,
                    payment_hash=result.payment_hash,
                    reservation_id=reservation_id,
                )
                anomalies = await check_for_anomalies(
                    payment_hash=result.payment_hash,
                    amount_sats=amount_sats,
                )
                if anomalies:
                    anomaly_note = (
                        f"\n\nAnomaly Detection: {len(anomalies)} flag(s) raised\n"
                        + "\n".join(f"  [{f.severity.upper()}] {f.flag_type}: {f.description}" for f in anomalies)
                    )
            except Exception as bookkeeping_err:
                import sys
                print(f"[pay_invoice] Bookkeeping error (payment DID succeed): {bookkeeping_err}", file=sys.stderr)
                anomaly_note = f"\n\n(Note: spending log write failed: {bookkeeping_err})"

            return [TextContent(
                type="text",
                text=(
                    f"Payment Successful!\n"
                    f"Amount: {amount_sats:,} sats\n"
                    f"Payment Hash: {result.payment_hash}\n"
                    f"Preimage (proof): {result.preimage}\n"
                    f"Routing Fee: {result.fee_msats / 1000:.1f} sats"
                    f"{anomaly_note}"
                ),
            )]
        else:
            # Payment failed — release the reservation
            if reservation_id:
                try:
                    await cancel_reservation(reservation_id)
                except Exception:
                    pass
            return [TextContent(
                type="text",
                text=(
                    f"Payment Failed\n"
                    f"Reason: {result.failure_reason}\n"
                    f"Payment Hash: {result.payment_hash}"
                ),
            )]

    elif name == "decode_invoice":
        decoded = lnd.decode_invoice(arguments["payment_request"])
        return [TextContent(
            type="text",
            text=(
                f"Invoice Details:\n"
                f"Destination: {decoded['destination']}\n"
                f"Amount: {decoded['amount_sats']:,} sats ({decoded['amount_msats']:,} msats)\n"
                f"Description: {decoded['description'] or '(none)'}\n"
                f"Payment Hash: {decoded['payment_hash']}\n"
                f"Expiry: {decoded['expiry']} seconds\n"
                f"Timestamp: {decoded['timestamp']}"
            ),
        )]

    elif name == "check_payment":
        result = lnd.lookup_invoice(arguments["payment_hash"])
        settled_text = "SETTLED" if result["settled"] else "PENDING"
        return [TextContent(
            type="text",
            text=(
                f"Payment Status: {settled_text}\n"
                f"Amount: {result['amount_msats'] // 1000:,} sats\n"
                f"Amount Paid: {result['amount_paid_msats'] // 1000:,} sats\n"
                f"Memo: {result['memo'] or '(none)'}\n"
                f"State: {result['state']}"
            ),
        )]

    return [TextContent(type="text", text=f"Unknown lightning tool: {name}")]


# =============================================================================
# Spending Status Tool
# =============================================================================


async def _get_spending_status() -> list[TextContent]:
    """Return current spending limits and usage."""
    summary = await get_spending_summary()
    return [TextContent(
        type="text",
        text=(
            f"Spending Limits Status\n"
            f"{'=' * 40}\n"
            f"Per-payment max: {summary['per_payment_limit_sats']:,} sats\n"
            f"Confirmation threshold: {summary['confirm_threshold_sats']:,} sats\n"
            f"\nHourly: {summary['spent_last_hour_sats']:,} / {summary['hourly_limit_sats']:,} sats"
            f" ({summary['hourly_remaining_sats']:,} remaining)\n"
            f"Daily:  {summary['spent_last_24h_sats']:,} / {summary['daily_limit_sats']:,} sats"
            f" ({summary['daily_remaining_sats']:,} remaining)"
        ),
    )]


def _create_macaroon(arguments: dict) -> list[TextContent]:
    """Create a scoped macaroon token."""
    profile = arguments.get("profile")
    permissions = arguments.get("permissions")

    try:
        token = derive_macaroon(profile=profile, permissions=permissions)
    except ValueError as e:
        return [TextContent(type="text", text=f"Error: {e}")]

    label = profile or "custom"
    # Show which tools this macaroon grants access to
    if profile and profile in PROFILES:
        granted = {p.value for p in PROFILES[profile]}
    elif permissions:
        granted = set(permissions)
    else:
        granted = set()

    allowed_tools = [t for t, p in TOOL_PERMISSIONS.items() if p.value in granted]

    return [TextContent(
        type="text",
        text=(
            f"Macaroon Created ({label} profile)\n"
            f"{'=' * 40}\n"
            f"Token: {token}\n"
            f"\nGranted permissions:\n"
            + "\n".join(f"  - {p}" for p in sorted(granted))
            + f"\n\nAllowed tools:\n"
            + "\n".join(f"  - {t}" for t in sorted(allowed_tools))
            + f"\n\nTo use: pass this token when connecting, or call "
            f"set_active_macaroon to switch to this scope."
        ),
    )]


def _list_permissions() -> list[TextContent]:
    """Show current session permissions."""
    active = get_active_permissions()

    if active is None:
        status = "No macaroon active (unrestricted mode)"
        perm_lines = "  All tools are accessible"
    else:
        status = f"{len(active)} permission(s) granted"
        perm_lines = "\n".join(f"  - {p.value}" for p in sorted(active, key=lambda x: x.value))

    # Build tool access map
    tool_access = []
    for tool_name, required_perm in sorted(TOOL_PERMISSIONS.items()):
        if active is None or required_perm in active:
            tool_access.append(f"  [allowed]  {tool_name}")
        else:
            tool_access.append(f"  [BLOCKED]  {tool_name}")

    return [TextContent(
        type="text",
        text=(
            f"Current Session Permissions\n"
            f"{'=' * 40}\n"
            f"Status: {status}\n"
            f"\nPermissions:\n{perm_lines}\n"
            f"\nTool Access:\n" + "\n".join(tool_access)
            + f"\n\nAvailable profiles: {', '.join(PROFILES.keys())}"
        ),
    )]


async def _get_anomaly_report() -> list[TextContent]:
    """Return anomaly detection summary."""
    summary = await get_anomaly_summary()

    if summary["total_flags"] == 0:
        return [TextContent(
            type="text",
            text="Anomaly Report: No flags detected. All transactions look clean.",
        )]

    lines = [
        f"Anomaly Detection Report",
        f"{'=' * 40}",
        f"Total flags: {summary['total_flags']}",
        f"Unreviewed: {summary['unreviewed']}",
        f"",
        f"By severity:",
        f"  High:   {summary['by_severity'].get('high', 0)}",
        f"  Medium: {summary['by_severity'].get('medium', 0)}",
        f"  Low:    {summary['by_severity'].get('low', 0)}",
    ]

    if summary["by_type"]:
        lines.append("")
        lines.append("By type:")
        for ftype, count in summary["by_type"].items():
            lines.append(f"  {ftype}: {count}")

    if summary["recent"]:
        lines.append("")
        lines.append("Recent flags:")
        for f in summary["recent"]:
            amount = f"{f['amount_sats']:,} sats" if f["amount_sats"] else "n/a"
            reviewed = " (reviewed)" if f["reviewed"] else ""
            lines.append(
                f"  [{f['severity'].upper()}] {f['type']} — {amount}{reviewed}"
            )
            lines.append(f"    {f['description']}")

    return [TextContent(type="text", text="\n".join(lines))]


# =============================================================================
# Marketplace Tool Implementations (PostgreSQL-backed)
# =============================================================================


async def _find_skill_by_id(session: AsyncSession, skill_id: str) -> Skill | None:
    """Find a skill by full or partial UUID."""
    # Try exact match first
    try:
        uid = uuid.UUID(skill_id)
        result = await session.execute(select(Skill).where(Skill.id == uid))
        skill = result.scalar_one_or_none()
        if skill:
            return skill
    except ValueError:
        pass

    # Partial ID match (cast UUID to text for LIKE query)
    from sqlalchemy import cast, String as SAString
    result = await session.execute(
        select(Skill).where(cast(Skill.id, SAString).like(f"{skill_id}%"))
    )
    return result.scalar_one_or_none()


async def _discover_skills(arguments: dict) -> list[TextContent]:
    """Search the skill marketplace."""
    query = arguments.get("query", "").lower()
    category = arguments.get("category", "").lower()
    max_price = arguments.get("max_price_sats", 0)

    async with async_session_factory() as session:
        stmt = select(Skill).where(Skill.is_active == True)

        if category:
            stmt = stmt.where(sa_func.lower(Skill.category) == category)

        if max_price > 0:
            stmt = stmt.where(Skill.price_sats <= max_price)

        if query:
            pattern = f"%{query}%"
            stmt = stmt.where(
                or_(
                    sa_func.lower(Skill.name).like(pattern),
                    sa_func.lower(Skill.description).like(pattern),
                    sa_func.lower(Skill.tags).like(pattern),
                )
            )

        result = await session.execute(stmt)
        skills = result.scalars().all()

    if not skills:
        return [TextContent(type="text", text="No skills found matching your criteria.")]

    lines = [f"Found {len(skills)} skill(s):\n"]
    for s in skills:
        badge = ""
        if s.verification_status == "fully_verified":
            badge = " [verified: node + domain]"
        elif s.verification_status == "node_verified":
            badge = " [verified: node]"
        elif s.verification_status == "domain_verified":
            badge = " [verified: domain]"
        lines.append(
            f"  [{str(s.id)}] {s.name}\n"
            f"    Category: {s.category} | Price: {s.price_sats} sats\n"
            f"    Provider: {s.provider_name}{badge}\n"
            f"    {s.description[:100]}\n"
        )

    return [TextContent(type="text", text="\n".join(lines))]


async def _get_skill_details(arguments: dict) -> list[TextContent]:
    """Get full details about a skill."""
    skill_id = arguments["skill_id"]

    async with async_session_factory() as session:
        skill = await _find_skill_by_id(session, skill_id)
        if not skill:
            return [TextContent(type="text", text=f"Skill not found: {skill_id}")]

        # Count ratings for this skill
        rating_count_stmt = (
            select(sa_func.count(Rating.id))
            .join(SkillExecution, Rating.execution_id == SkillExecution.id)
            .where(SkillExecution.skill_id == skill.id)
        )
        result = await session.execute(rating_count_stmt)
        rating_count = result.scalar() or 0

        rating_text = (
            f"{float(skill.avg_rating):.1f}/5.0 ({rating_count} ratings)"
            if rating_count > 0 else "No ratings yet"
        )

        return [TextContent(
            type="text",
            text=(
                f"Skill: {skill.name}\n"
                f"ID: {skill.id}\n"
                f"Category: {skill.category}\n"
                f"Tags: {skill.tags or 'none'}\n"
                f"Price: {skill.price_sats} sats\n"
                f"Provider: {skill.provider_name}\n"
                f"Lightning Address: {skill.provider_lightning_address or 'not set'}\n"
                f"Rating: {rating_text}\n"
                f"Total Executions: {skill.total_executions}\n"
                f"\nDescription: {skill.description}\n"
                f"\nInput Schema: {json.dumps(skill.input_schema or {}, indent=2)}\n"
                f"Output Schema: {json.dumps(skill.output_schema or {}, indent=2)}"
            ),
        )]


async def _register_skill(arguments: dict) -> list[TextContent]:
    """Register a new skill on the marketplace."""
    async with async_session_factory() as session:
        skill = Skill(
            name=arguments["name"],
            description=arguments["description"],
            category=arguments["category"],
            tags=arguments.get("tags", ""),
            price_sats=arguments["price_sats"],
            provider_name=arguments["provider_name"],
            provider_pubkey=arguments.get("provider_pubkey"),
            provider_lightning_address=arguments["provider_lightning_address"],
            input_schema=arguments.get("input_schema", {}),
            output_schema=arguments.get("output_schema", {}),
            total_executions=0,
            avg_rating=0.0,
            is_active=True,
        )
        session.add(skill)
        await session.commit()
        await session.refresh(skill)

        return [TextContent(
            type="text",
            text=(
                f"Skill Registered!\n"
                f"ID: {skill.id}\n"
                f"Name: {skill.name}\n"
                f"Price: {skill.price_sats} sats\n"
                f"Lightning Address: {skill.provider_lightning_address}\n"
                f"\nYour skill is now discoverable on the Conduit marketplace.\n"
                f"Consumers will pay you directly at your Lightning address."
            ),
        )]


async def _request_skill_execution(arguments: dict) -> list[TextContent]:
    """Request a skill execution — creates invoice(s) for payment."""
    skill_id = arguments["skill_id"]

    async with async_session_factory() as session:
        skill = await _find_skill_by_id(session, skill_id)
        if not skill:
            return [TextContent(type="text", text=f"Skill not found: {skill_id}")]

        lnd = get_lnd()
        fee = calculate_fee(skill.price_sats)

        # Invoice 1: skill price (paid to provider via our node)
        invoice = lnd.create_invoice(
            amount_msats=skill.price_sats * 1000,
            memo=f"Conduit Skill: {skill.name}",
            expiry=600,  # 10 min to pay
        )

        # Invoice 2: platform fee (paid to our node, if fee > 0)
        fee_payment_hash = None
        fee_payment_request = None
        if fee.fee_enabled:
            fee_invoice = lnd.create_invoice(
                amount_msats=fee.platform_fee_sats * 1000,
                memo=f"Conduit platform fee: {skill.name}",
                expiry=600,
            )
            fee_payment_hash = fee_invoice.payment_hash
            fee_payment_request = fee_invoice.payment_request

        # Create execution record in database
        execution = SkillExecution(
            skill_id=skill.id,
            consumer_name=arguments.get("consumer_name", "anonymous"),
            input_data=arguments.get("input_data", {}),
            payment_hash=invoice.payment_hash,
            amount_sats=skill.price_sats,
            platform_fee_sats=fee.platform_fee_sats,
            fee_payment_hash=fee_payment_hash,
            fee_payment_request=fee_payment_request,
            fee_settled=False,
            status=ExecutionStatus.PENDING_PAYMENT,
        )
        session.add(execution)
        await session.commit()
        await session.refresh(execution)

        # Build response with fee breakdown
        fee_text = ""
        if fee.fee_enabled:
            fee_text = (
                f"\nPlatform Fee Invoice ({fee.platform_fee_sats} sats):\n"
                f"Fee Payment Hash: {fee_payment_hash}\n"
                f"Fee Payment Request: {fee_payment_request}\n"
            )

        return [TextContent(
            type="text",
            text=(
                f"Skill Execution Requested!\n"
                f"Skill: {skill.name} by {skill.provider_name}\n"
                f"Price: {skill.price_sats} sats\n"
                f"Platform fee: {fee.platform_fee_sats} sats ({fee.fee_percent}%)\n"
                f"Total cost: {fee.total_consumer_cost_sats} sats\n"
                f"Execution ID: {execution.id}\n"
                f"\nSkill Invoice ({skill.price_sats} sats):\n"
                f"Payment Hash: {invoice.payment_hash}\n"
                f"Payment Request: {invoice.payment_request}\n"
                f"{fee_text}"
                f"\nPay {'both invoices' if fee.fee_enabled else 'this invoice'} to proceed.\n"
                f"Use check_payment with the payment hash to verify settlement."
            ),
        )]


async def _confirm_skill_execution(arguments: dict) -> list[TextContent]:
    """Confirm payment and trigger skill execution via provider webhook."""
    exec_id = arguments["execution_id"]
    preimage = arguments["payment_preimage"]

    async with async_session_factory() as session:
        # Find the execution
        try:
            uid = uuid.UUID(exec_id)
        except ValueError:
            return [TextContent(type="text", text=f"Invalid execution ID: {exec_id}")]

        # Lock the row to prevent concurrent confirm calls (H6)
        result = await session.execute(
            select(SkillExecution)
            .where(SkillExecution.id == uid)
            .with_for_update()
        )
        execution = result.scalar_one_or_none()
        if not execution:
            return [TextContent(type="text", text=f"Execution not found: {exec_id}")]

        if execution.status != ExecutionStatus.PENDING_PAYMENT:
            return [TextContent(
                type="text",
                text=f"Execution is not awaiting payment (status: {execution.status.value})",
            )]

        # C1: Verify preimage proves payment before any state mutation
        if execution.payment_hash:
            try:
                preimage_bytes = bytes.fromhex(preimage)
            except ValueError:
                return [TextContent(
                    type="text",
                    text="Invalid preimage: must be a hex string.",
                )]
            computed_hash = hashlib.sha256(preimage_bytes).hexdigest()
            if computed_hash != execution.payment_hash:
                return [TextContent(
                    type="text",
                    text="Payment preimage does not match payment hash.",
                )]

        # Verify skill payment settled
        lnd = get_lnd()
        invoice_status = lnd.lookup_invoice(execution.payment_hash)
        if not invoice_status["settled"]:
            return [TextContent(
                type="text",
                text=(
                    f"Skill payment has not settled yet.\n"
                    f"Payment hash: {execution.payment_hash}\n"
                    f"Status: PENDING\n\n"
                    f"Pay the skill invoice first, then try again."
                ),
            )]

        # Verify platform fee invoice settled (if applicable)
        if execution.fee_payment_hash and execution.platform_fee_sats > 0:
            fee_status = lnd.lookup_invoice(execution.fee_payment_hash)
            if not fee_status["settled"]:
                return [TextContent(
                    type="text",
                    text=(
                        f"Platform fee has not been paid yet.\n"
                        f"Fee payment hash: {execution.fee_payment_hash}\n"
                        f"Fee amount: {execution.platform_fee_sats} sats\n\n"
                        f"Pay both the skill invoice and the fee invoice, then try again."
                    ),
                )]
            execution.fee_settled = True

        # All payments confirmed -- update status
        execution.payment_preimage = preimage
        execution.status = ExecutionStatus.PAYMENT_RECEIVED

        # Look up the skill to get the endpoint_url
        skill_result = await session.execute(
            select(Skill).where(Skill.id == execution.skill_id)
        )
        skill = skill_result.scalar_one_or_none()
        if not skill:
            execution.status = ExecutionStatus.FAILED
            execution.error_message = "Skill not found in registry"
            await session.commit()
            return [TextContent(type="text", text="Error: skill no longer exists in registry")]

        # Run anomaly detection on every confirmed execution
        anomalies = await check_for_anomalies(
            payment_hash=execution.payment_hash,
            execution_id=str(execution.id),
            consumer_name=execution.consumer_name,
            provider_name=skill.provider_name,
            skill_id=str(skill.id),
            amount_sats=execution.amount_sats,
        )
        anomaly_note = ""
        if anomalies:
            anomaly_note = (
                f"\n\nAnomaly Detection: {len(anomalies)} flag(s) raised\n"
                + "\n".join(f"  [{f.severity.upper()}] {f.flag_type}: {f.description}" for f in anomalies)
            )

        # Check if provider has a webhook endpoint
        if not skill.endpoint_url:
            execution.status = ExecutionStatus.COMPLETED
            execution.output_data = {
                "message": f"Payment of {execution.amount_sats} sats confirmed for '{skill.name}'.",
                "note": "This skill has no execution endpoint configured. "
                        "The provider needs to register an endpoint_url to enable automatic execution.",
                "payment_proof": {
                    "payment_hash": execution.payment_hash,
                    "payment_preimage": preimage,
                },
            }
            await session.commit()
            return [TextContent(
                type="text",
                text=(
                    f"Payment Confirmed! ({execution.amount_sats} sats)\n"
                    f"Skill: {skill.name}\n\n"
                    f"Note: This skill has no execution endpoint configured.\n"
                    f"Payment proof has been recorded. The provider would need to "
                    f"register an endpoint_url for automatic execution.\n\n"
                    f"Payment hash: {execution.payment_hash}\n"
                    f"Preimage: {preimage}"
                    f"{anomaly_note}"
                ),
            )]

        # Execute via webhook
        execution.status = ExecutionStatus.EXECUTING
        await session.commit()

        try:
            webhook_result = await execute_skill_webhook(
                endpoint_url=skill.endpoint_url,
                input_data=execution.input_data or {},
                payment_hash=execution.payment_hash,
                payment_preimage=preimage,
                skill_name=skill.name,
                execution_id=str(execution.id),
            )

            # Success — store output
            execution.status = ExecutionStatus.COMPLETED
            execution.output_data = webhook_result.get("output", webhook_result)
            execution.execution_time_ms = webhook_result.get("execution_time_ms")

            # Update skill stats
            skill.total_executions = (skill.total_executions or 0) + 1
            await session.commit()

            output_text = json.dumps(execution.output_data, indent=2)
            return [TextContent(
                type="text",
                text=(
                    f"Skill Executed Successfully!\n"
                    f"Skill: {skill.name} by {skill.provider_name}\n"
                    f"Execution time: {execution.execution_time_ms}ms\n"
                    f"{'=' * 40}\n"
                    f"Output:\n{output_text}"
                    f"{anomaly_note}"
                ),
            )]

        except SkillExecutionError as e:
            execution.status = ExecutionStatus.FAILED
            execution.error_message = e.reason
            await session.commit()
            return [TextContent(
                type="text",
                text=(
                    f"Skill Execution Failed\n"
                    f"Skill: {skill.name}\n"
                    f"Error: {e.reason}\n\n"
                    f"Payment was received but execution failed.\n"
                    f"Execution ID: {execution.id}\n"
                    f"Contact the provider for a refund."
                ),
            )]


async def _submit_rating(arguments: dict) -> list[TextContent]:
    """Submit a rating for a skill execution (with integrity checks)."""
    exec_id = arguments["execution_id"]
    score = arguments["score"]
    preimage = arguments["payment_preimage"]
    comment = arguments.get("comment", "")

    if score < 1 or score > 5:
        return [TextContent(type="text", text="Score must be between 1 and 5.")]

    async with async_session_factory() as session:
        # Find the execution
        try:
            uid = uuid.UUID(exec_id)
        except ValueError:
            return [TextContent(type="text", text=f"Invalid execution ID: {exec_id}")]

        result = await session.execute(
            select(SkillExecution).where(SkillExecution.id == uid)
        )
        execution = result.scalar_one_or_none()
        if not execution:
            return [TextContent(type="text", text=f"Execution not found: {exec_id}")]

        # Look up the skill
        skill_result = await session.execute(
            select(Skill).where(Skill.id == execution.skill_id)
        )
        skill = skill_result.scalar_one_or_none()

        # --- Integrity checks ---
        try:
            await validate_rating(session, execution, preimage, skill)
        except RatingIntegrityError as e:
            return [TextContent(type="text", text=f"Rating rejected: {e}")]

        # Store rating
        rating = Rating(
            execution_id=execution.id,
            score=score,
            comment=comment,
            payment_preimage=preimage,
            rater_name=execution.consumer_name,
        )
        session.add(rating)

        # Update skill with weighted average rating
        if skill:
            # Flush the new rating so it's visible to the weighted calc
            await session.flush()
            weighted_avg = await calculate_weighted_rating(session, skill.id)
            skill.avg_rating = weighted_avg

            # Check for rating concentration (one consumer dominating reviews)
            concentration_flag = await check_provider_rating_concentration(
                session, skill, execution.consumer_name,
            )
            if concentration_flag:
                session.add(concentration_flag)

        await session.commit()

        skill_name = skill.name if skill else "Unknown"
        weighted_note = f" (weighted avg: {skill.avg_rating}/5.0)" if skill else ""
        return [TextContent(
            type="text",
            text=(
                f"Rating Submitted!\n"
                f"Skill: {skill_name}{weighted_note}\n"
                f"Score: {'★' * score}{'☆' * (5 - score)} ({score}/5)\n"
                f"Comment: {comment or '(none)'}\n"
                f"Verified by payment proof: {preimage[:16]}...\n"
                f"\nIntegrity checks passed: preimage verified, no duplicates, timing OK."
            ),
        )]


# =============================================================================
# Verification Handlers
# =============================================================================


async def _request_verification(arguments: dict) -> list[TextContent]:
    """Start a verification challenge for a skill provider."""
    skill_id = arguments["skill_id"]
    method = arguments["method"]
    domain = arguments.get("domain", "")

    async with async_session_factory() as session:
        try:
            if method == "node":
                challenge = await start_node_verification(session, skill_id)
                return [TextContent(
                    type="text",
                    text=(
                        f"Node Verification Challenge\n"
                        f"──────────────────────────────\n"
                        f"Sign this message with your Lightning node:\n\n"
                        f"  lncli signmessage \"{challenge}\"\n\n"
                        f"Then call submit_verification with the signature.\n"
                        f"This proves you control the node's private key."
                    ),
                )]
            elif method == "domain":
                if not domain:
                    return [TextContent(
                        type="text",
                        text="Domain is required for domain verification.",
                    )]
                challenge = await start_domain_verification(session, skill_id, domain)
                return [TextContent(
                    type="text",
                    text=(
                        f"Domain Verification Challenge\n"
                        f"──────────────────────────────\n"
                        f"Place this text at:\n"
                        f"  https://{domain}/.well-known/conduit-verify.txt\n\n"
                        f"Content:\n  {challenge}\n\n"
                        f"Then call submit_verification with method='domain' "
                        f"and domain='{domain}'."
                    ),
                )]
            else:
                return [TextContent(type="text", text=f"Unknown method: {method}")]
        except VerificationError as e:
            return [TextContent(type="text", text=f"Verification error: {e}")]


async def _submit_verification(arguments: dict) -> list[TextContent]:
    """Complete a pending verification."""
    skill_id = arguments["skill_id"]
    method = arguments["method"]

    async with async_session_factory() as session:
        try:
            if method == "node":
                signature = arguments.get("signature", "")
                if not signature:
                    return [TextContent(
                        type="text",
                        text="Signature is required for node verification.",
                    )]
                result = await verify_node_signature(session, skill_id, signature, lnd=get_lnd())
                badge = "🟢" if result["status"] == "fully_verified" else "🔵"
                return [TextContent(
                    type="text",
                    text=(
                        f"{badge} Node Verified!\n"
                        f"Skill: {result['skill_name']} by {result['provider']}\n"
                        f"Node pubkey: {result['pubkey']}\n"
                        f"Status: {result['status']}\n\n"
                        f"This skill is now backed by a real Lightning node."
                    ),
                )]
            elif method == "domain":
                domain = arguments.get("domain", "")
                if not domain:
                    return [TextContent(
                        type="text",
                        text="Domain is required for domain verification.",
                    )]
                result = await verify_domain(session, skill_id, domain)
                badge = "🟢" if result["status"] == "fully_verified" else "🌐"
                return [TextContent(
                    type="text",
                    text=(
                        f"{badge} Domain Verified!\n"
                        f"Skill: {result['skill_name']} by {result['provider']}\n"
                        f"Domain: {result['domain']}\n"
                        f"Status: {result['status']}\n\n"
                        f"This provider has proven control of {result['domain']}."
                    ),
                )]
            else:
                return [TextContent(type="text", text=f"Unknown method: {method}")]
        except VerificationError as e:
            return [TextContent(type="text", text=f"Verification failed: {e}")]


async def _get_verification_status(arguments: dict) -> list[TextContent]:
    """Get verification status for a skill."""
    skill_id = arguments["skill_id"]

    async with async_session_factory() as session:
        try:
            status = await get_verification_status(session, skill_id)

            badges = []
            if status["verified_node_pubkey"]:
                badges.append(f"🔵 Node: {status['verified_node_pubkey'][:16]}...")
            if status["verified_domain"]:
                badges.append(f"🌐 Domain: {status['verified_domain']}")
            if not badges:
                badges.append("⚪ Unverified")

            return [TextContent(
                type="text",
                text=(
                    f"Verification Status: {status['skill_name']}\n"
                    f"Provider: {status['provider']}\n"
                    f"Status: {status['verification_status']}\n"
                    f"{''.join(chr(10) + b for b in badges)}\n"
                    f"{'Verified: ' + status['verified_at'] if status['verified_at'] else ''}"
                ),
            )]
        except VerificationError as e:
            return [TextContent(type="text", text=f"Error: {e}")]


# =============================================================================
# Nostr Tool Implementations
# =============================================================================


async def _nostr_publish_skill(arguments: dict) -> list[TextContent]:
    """Publish a skill from the local marketplace to Nostr relays."""
    skill_id = arguments["skill_id"]
    relay_override = arguments.get("relays", [])

    # Load the skill from database
    async with async_session_factory() as session:
        skill = await _find_skill_by_id(session, skill_id)
        if not skill:
            return [TextContent(type="text", text=f"Skill not found: {skill_id}")]

        # Build skill dict for Nostr event
        skill_dict = {
            "id": str(skill.id),
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "tags": skill.tags or "",
            "price_sats": skill.price_sats,
            "provider_name": skill.provider_name,
            "provider_lightning_address": skill.provider_lightning_address or "",
            "input_schema": skill.input_schema,
            "output_schema": skill.output_schema,
            "endpoint_url": skill.endpoint_url or "",
        }

    # Sign the event
    keys = get_nostr_keys()
    event = skill_to_event(skill_dict, keys)

    # Publish to relays
    relays = get_nostr_relays(relay_override if relay_override else None)
    results = await publish_to_relays(event, relays)

    # Format results
    ok_count = sum(1 for v in results.values() if v)
    relay_lines = []
    for url, ok in results.items():
        status = "published" if ok else "FAILED"
        relay_lines.append(f"  {url}: {status}")

    return [TextContent(
        type="text",
        text=(
            f"Nostr Skill Published!\n"
            f"{'=' * 40}\n"
            f"Skill: {skill_dict['name']}\n"
            f"Event ID: {event.id}\n"
            f"Pubkey: {keys.pubkey_hex}\n"
            f"Kind: {event.kind}\n"
            f"\nRelays ({ok_count}/{len(results)} accepted):\n"
            + "\n".join(relay_lines)
            + f"\n\nThis skill is now discoverable on Nostr by any agent.\n"
            f"Event is replaceable (kind {SKILL_EVENT_KIND}) — re-publish to update."
        ),
    )]


async def _nostr_discover_skills(arguments: dict) -> list[TextContent]:
    """Discover skills on Nostr relays."""
    category = arguments.get("category", "")
    max_price = arguments.get("max_price_sats", 0)
    relay_override = arguments.get("relays", [])

    from conduit.core.config import settings
    relays = get_nostr_relays(relay_override if relay_override else None)
    window = settings.nostr_discovery_window_hours

    skills = await discover_from_relays(
        relay_urls=relays,
        category=category,
        max_price_sats=max_price,
        since_hours=window,
        limit=50,
    )

    if not skills:
        return [TextContent(
            type="text",
            text=(
                f"No Conduit skills found on Nostr relays.\n"
                f"Searched {len(relays)} relay(s) for kind {SKILL_EVENT_KIND} events "
                f"in the last {window} hours.\n"
                f"Relays: {', '.join(relays)}"
            ),
        )]

    lines = [f"Found {len(skills)} skill(s) on Nostr:\n"]
    for s in skills:
        lines.append(
            f"  [{s.get('nostr_event_id', '')[:12]}...] {s['name']}\n"
            f"    Category: {s['category']} | Price: {s['price_sats']} sats\n"
            f"    Provider: {s['provider_name']}\n"
            f"    Lightning: {s.get('provider_lightning_address', 'not set')}\n"
            f"    Nostr pubkey: {s['nostr_pubkey'][:16]}...\n"
            f"    Relay: {s.get('relay', 'unknown')}\n"
            f"    {s['description'][:100]}\n"
        )

    return [TextContent(type="text", text="\n".join(lines))]


async def _nostr_get_profile() -> list[TextContent]:
    """Show the Nostr identity for this Conduit node."""
    keys = get_nostr_keys()
    from conduit.core.config import settings
    relays = settings.nostr_relay_list

    # Count locally registered skills
    skill_count = 0
    try:
        async with async_session_factory() as session:
            result = await session.execute(
                select(sa_func.count(Skill.id)).where(Skill.is_active == True)
            )
            skill_count = result.scalar() or 0
    except Exception:
        pass

    key_status = "configured (persisted)" if settings.nostr_private_key else "auto-generated (NOT persisted — will change on restart)"
    persist_note = ""
    if not settings.nostr_private_key:
        # H9: Never print nsec to stderr (it gets captured by log shippers).
        # Write it to a 0600 file in credentials/ so only the operator can read it.
        import os as _os
        from pathlib import Path as _Path
        creds_dir = _Path(__file__).resolve().parent.parent.parent / "credentials"
        creds_dir.mkdir(exist_ok=True)
        nsec_file = creds_dir / "nostr.nsec"
        nsec_file.write_text(keys.nsec + "\n")
        _os.chmod(nsec_file, 0o600)
        print(
            f"[nostr] Auto-generated identity saved to {nsec_file} (mode 0600). "
            f"To persist, add NOSTR_PRIVATE_KEY to .env.",
            file=sys.stderr,
        )
        persist_note = (
            f"\n\nWARNING: Key is not persisted. The nsec was saved to "
            f"credentials/nostr.nsec (readable only by the current user). "
            f"Add NOSTR_PRIVATE_KEY to .env to persist across restarts."
        )

    return [TextContent(
        type="text",
        text=(
            f"Nostr Identity\n"
            f"{'=' * 40}\n"
            f"Public Key (hex): {keys.pubkey_hex}\n"
            f"npub: {keys.npub}\n"
            f"Key source: {key_status}\n"
            f"\nConfigured Relays:\n"
            + "\n".join(f"  - {r}" for r in relays)
            + f"\n\nLocal skills available to publish: {skill_count}"
            f"{persist_note}"
        ),
    )]


async def _nostr_relay_status(arguments: dict) -> list[TextContent]:
    """Check connectivity to Nostr relays."""
    import asyncio
    relay_override = arguments.get("relays", [])
    relays = get_nostr_relays(relay_override if relay_override else None)

    results: dict[str, str] = {}

    async def _check_one(url: str):
        try:
            async with NostrRelay(url, timeout=5.0) as relay:
                results[url] = "connected"
        except ImportError:
            results[url] = "websockets package not installed"
        except Exception as e:
            results[url] = f"error: {type(e).__name__}: {str(e)}"

    await asyncio.gather(*[_check_one(url) for url in relays])

    ok_count = sum(1 for v in results.values() if v == "connected")
    lines = [
        f"Nostr Relay Status ({ok_count}/{len(results)} connected)\n"
        f"{'=' * 40}",
    ]
    for url, status in results.items():
        icon = "🟢" if status == "connected" else "🔴"
        lines.append(f"  {icon} {url}: {status}")

    return [TextContent(type="text", text="\n".join(lines))]


# =============================================================================
# L402 Tool Handlers
# =============================================================================


async def _create_l402_token(arguments: dict) -> list[TextContent]:
    """Create an L402 access token (macaroon + invoice)."""
    from conduit.services.l402 import create_l402_challenge

    amount_sats = arguments.get("amount_sats")
    if not amount_sats or amount_sats <= 0:
        return [TextContent(type="text", text="Error: amount_sats must be > 0")]

    memo = arguments.get("memo", "Conduit L402 access")
    resource = arguments.get("resource")
    expiry = arguments.get("expiry_seconds")

    try:
        lnd = get_lnd()
        challenge = create_l402_challenge(
            lnd,
            amount_sats=amount_sats,
            memo=memo,
            resource=resource,
            expiry_seconds=expiry,
        )
    except Exception as e:
        return [TextContent(type="text", text=f"Error creating L402 token: {e}")]

    return [TextContent(
        type="text",
        text=(
            f"L402 Token Created\n"
            f"{'=' * 40}\n"
            f"Amount: {challenge.amount_sats} sats\n"
            f"Payment Hash: {challenge.payment_hash}\n"
            f"Expires: {challenge.expires_at}\n"
            f"\nInvoice (pay this):\n{challenge.invoice}\n"
            f"\nMacaroon (present after payment):\n{challenge.macaroon}\n"
            f"\nAfter paying, use:\n"
            f"  Authorization: L402 {challenge.macaroon}:<preimage_hex>"
        ),
    )]


async def _verify_l402_token(arguments: dict) -> list[TextContent]:
    """Verify an L402 credential (macaroon + preimage)."""
    from conduit.services.l402 import L402Credential, verify_l402

    macaroon = arguments.get("macaroon", "")
    preimage = arguments.get("preimage", "")

    if not macaroon or not preimage:
        return [TextContent(type="text", text="Error: both macaroon and preimage are required")]

    if len(preimage) != 64:
        return [TextContent(type="text", text="Error: preimage must be 64 hex characters (32 bytes)")]

    credential = L402Credential(macaroon_raw=macaroon, preimage=preimage)
    result = verify_l402(credential)

    if result.valid:
        return [TextContent(
            type="text",
            text=(
                f"L402 Verification: VALID\n"
                f"{'=' * 40}\n"
                f"Payment Hash: {result.payment_hash}\n"
                f"Resource: {result.resource or '(unrestricted)'}\n"
                f"Payment proven — preimage matches payment_hash."
            ),
        )]
    else:
        return [TextContent(
            type="text",
            text=(
                f"L402 Verification: INVALID\n"
                f"{'=' * 40}\n"
                f"Error: {result.error}"
            ),
        )]


async def _get_l402_status() -> list[TextContent]:
    """Get L402 configuration status."""
    from conduit.core.config import settings

    return [TextContent(
        type="text",
        text=(
            f"L402 Configuration\n"
            f"{'=' * 40}\n"
            f"Enabled: {settings.l402_enabled}\n"
            f"Default Price: {settings.l402_default_price_sats} sats\n"
            f"Token Expiry: {settings.l402_token_expiry_seconds} seconds\n"
            f"Free Routes: {', '.join(settings.l402_free_route_list)}\n"
            f"\nWhen enabled, endpoints accept either:\n"
            f"  - X-API-Key header (existing auth)\n"
            f"  - Authorization: L402 <macaroon>:<preimage> (pay-per-request)"
        ),
    )]


# =============================================================================
# Entry Point
# =============================================================================


def _check_api_key():
    """Verify API key is set before starting. Exits if missing or default."""
    from conduit.core.config import settings
    key = settings.conduit_api_key
    if not key or key == "CHANGE-ME":
        print(
            "FATAL: CONDUIT_API_KEY is not set or still using the default value.\n"
            "Generate a key with: python3 -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "Then set CONDUIT_API_KEY in your .env file.",
            file=sys.stderr,
        )
        sys.exit(1)
    # Don't echo any portion of the key — even 6 leading chars narrows
    # a brute-force search.
    print("API key configured.", file=sys.stderr)


def _check_secret_file_permissions():
    """
    Refuse to start if .env or LND credentials are world/group readable.
    A leaked admin macaroon means total control of the Lightning node.
    """
    import stat
    from conduit.core.config import settings

    project_root = Path(__file__).resolve().parent.parent.parent
    paths = [
        project_root / ".env",
        Path(settings.lnd_macaroon_path).expanduser(),
        Path(settings.lnd_tls_cert_path).expanduser(),
    ]
    creds_dir = project_root / "credentials"
    if creds_dir.is_dir():
        paths.extend(creds_dir.iterdir())

    bad: list[str] = []
    for p in paths:
        try:
            if not p.exists() or not p.is_file():
                continue
            mode = p.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                bad.append(f"{p}  mode={oct(mode & 0o777)}")
        except Exception:
            continue

    if bad:
        print(
            "WARNING: secret files are world/group accessible. "
            "Fix with: chmod 600 <file>\n  " + "\n  ".join(bad),
            file=sys.stderr,
        )
        if settings.is_production:
            sys.exit(1)


async def main():
    """Run the MCP server over stdio."""
    _check_api_key()
    _check_secret_file_permissions()
    # Initialize with root (admin) macaroon — full permissions for local session
    initialize_root_session()
    print("Root macaroon initialized (admin permissions)", file=sys.stderr)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
