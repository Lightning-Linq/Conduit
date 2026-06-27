# Conduit

**Lightning Payment Rails for AI Agents** | by [Lightning Linq](https://lightninglinq.ai)

Conduit is a non-custodial payment infrastructure layer that lets AI agents transact over the Lightning Network. It exposes a skill marketplace and Lightning tools via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io), enabling any MCP-compatible AI (like Claude) to discover, purchase, and rate agent-provided services -- all settled instantly in Bitcoin.

Conduit never takes custody of funds. Payments flow directly between agents on Lightning. Conduit provides coordination, discovery, reputation, and security -- never custody.

> Conduit is the first product from **Lightning Linq**, an open-source company building Lightning infrastructure for AI agents.

## How It Works

```
┌──────────────────────────────────────────────────────────┐
│                     Claude Desktop                        │
│                  (or any MCP client)                      │
└──────────────┬───────────────────────────────────────────┘
               │ MCP (stdio)
               ▼
┌──────────────────────────────────────────────────────────┐
│                   Conduit MCP Server                      │
│                                                           │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────┐ │
│  │  Lightning   │  │  Marketplace │  │    Security      │ │
│  │   Tools      │  │    Tools     │  │    Layer         │ │
│  │             │  │              │  │                   │ │
│  │ • invoices  │  │ • discover   │  │ • API key auth   │ │
│  │ • payments  │  │ • register   │  │ • macaroons      │ │
│  │ • balance   │  │ • execute    │  │ • spending limits │ │
│  │ • decode    │  │ • rate       │  │ • rate limiting   │ │
│  │             │  │ • verify     │  │ • anomaly detect  │ │
│  └──────┬──────┘  └──────┬───────┘  └─────────────────┘ │
│         │                │                                │
└─────────┼────────────────┼────────────────────────────────┘
          │                │
          ▼                ▼
┌──────────────┐   ┌──────────────┐
│   LND Node   │   │  PostgreSQL  │
│  (your node) │   │  (local DB)  │
│              │   │              │
│  non-custodial   │  skills,     │
│  payments    │   │  executions, │
│              │   │  ratings,    │
│              │   │  audit logs  │
└──────────────┘   └──────────────┘
```

## Features

**Lightning Network Integration** — Create and pay invoices via your own LND node. Decode payment requests, check payment status, view node info and channel balances. Non-custodial: your keys, your node, your sats.

**Skill Marketplace** — Register skills with pricing, categories, and input/output schemas. Discover skills by keyword, category, or price range. Request executions with automatic Lightning invoicing. Webhook-based execution engine with payment proof delivery. Rating system backed by cryptographic payment proofs.

**Security Stack** — API key authentication, scoped macaroon authorization (10 permissions, 4 profiles), per-payment/hourly/daily spending limits, in-memory sliding window rate limiting, anomaly detection (self-payment, rapid repeat, structuring, volume spike), rating integrity (preimage verification, duplicate prevention, weighted averages), and provider verification via Lightning node signatures and domain proof.

**Federated Reputation** — Ratings are payer-signed, provider-bound attestations published over Nostr, so a skill's reputation is verifiable across nodes rather than siloed per server. Sybil-resistant aggregation (distinct-payer weighting, self-deal exclusion, payer web-of-trust) with a local Postgres cache. Opt-out via `FEDERATION_ENABLED`.

## Quick Start

Conduit is published on **PyPI** and **npm**:

```bash
pip install conduit-lightning    # the server — installs the `conduit-mcp` and `conduit-api` commands
npx conduit-setup                # interactive wizard that configures your AI client
```

For a complete local setup — PostgreSQL, database migrations, a generated API key, and Claude Desktop wiring — use the install script, which handles everything end to end:

```bash
git clone https://github.com/Lightning-Linq/conduit.git
cd conduit
chmod +x install.sh
./install.sh
```

The install script checks prerequisites (Python 3.11+, PostgreSQL), creates a virtual environment, installs dependencies, generates a secure API key, sets up the database, runs migrations, and shows you how to wire it into Claude Desktop.

### Prerequisites

- **Python 3.11+** — `brew install python@3.11` or use pyenv
- **PostgreSQL 16** — `brew install postgresql@16 && brew services start postgresql@16`
- **LND node** — running and accessible (local, remote, or via Tor)

### Claude Desktop Configuration

Add this to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "conduit-lightning": {
      "command": "/path/to/conduit/.venv/bin/python",
      "args": ["-m", "conduit.mcp_server"],
      "env": {
        "PYTHONPATH": "/path/to/conduit/src"
      }
    }
  }
}
```

Restart Claude Desktop. Ask Claude: *"What's my Lightning node balance?"*

## MCP Tools Reference

Conduit exposes 27 tools over the Model Context Protocol.

### Lightning Tools

| Tool | Permission | Description |
|------|-----------|-------------|
| `get_node_info` | lightning:read | Node alias, pubkey, active channels, peers |
| `get_balance` | lightning:read | On-chain and channel balances |
| `create_invoice` | lightning:invoice | Generate a Lightning invoice |
| `pay_invoice` | lightning:pay | Pay a Lightning invoice (with spending limits) |
| `decode_invoice` | lightning:read | Decode a payment request without paying |
| `check_payment` | lightning:read | Check if a payment has settled |

### Marketplace Tools

| Tool | Permission | Description |
|------|-----------|-------------|
| `discover_skills` | marketplace:read | Search skills by keyword, category, price |
| `get_skill_details` | marketplace:read | Full details including schemas and ratings |
| `register_skill` | marketplace:write | List a new skill on the marketplace |
| `request_skill_execution` | marketplace:execute | Request execution (generates invoice) |
| `confirm_skill_execution` | marketplace:execute | Confirm payment and trigger webhook |
| `submit_rating` | marketplace:execute | Rate a skill (requires payment proof) |
| `report_skill` | marketplace:execute | Report a skill for abuse/fraud (payment-proof backed) |

### Verification Tools

| Tool | Permission | Description |
|------|-----------|-------------|
| `request_verification` | marketplace:write | Start node or domain verification |
| `submit_verification` | marketplace:write | Complete verification with proof |
| `get_verification_status` | marketplace:read | Check a skill's verification badges |

### Security Tools

| Tool | Permission | Description |
|------|-----------|-------------|
| `get_spending_status` | security:read | Current spending vs. limits |
| `create_macaroon` | security:admin | Mint a scoped authorization token |
| `list_permissions` | security:read | Show active permissions |
| `get_anomaly_report` | security:read | View flagged suspicious patterns |

### Nostr Tools

| Tool | Permission | Description |
|------|-----------|-------------|
| `nostr_discover_skills` | nostr:read | Discover skills published to Nostr relays (NIP-33) |
| `nostr_publish_skill` | nostr:write | Publish a skill to Nostr for decentralized discovery |
| `nostr_get_profile` | nostr:read | Fetch a provider's Nostr profile (NIP-01) |
| `nostr_relay_status` | nostr:read | Check connectivity of configured relays |

### L402 Tools

| Tool | Permission | Description |
|------|-----------|-------------|
| `create_l402_token` | lightning:invoice | Mint an L402 (HTTP 402) token bound to an invoice |
| `verify_l402_token` | security:read | Verify an L402 token and its preimage |
| `get_l402_status` | security:read | Check L402 challenge/token status |

## Security Model

Conduit uses defense-in-depth with multiple security layers.

**Authentication** — An API key is required to start the server. Without it, the MCP server refuses to run.

**Authorization** — Macaroon-based scoping with 10 permission levels. Create restricted tokens for specific use cases (read-only, marketplace-only, spending-only).

**Spending Controls** — Configurable per-payment limits (default 10,000 sats), hourly caps (50,000 sats), daily caps (200,000 sats), and confirmation prompts for payments above a threshold.

**Rate Limiting** — Per-tool sliding window rate limits. Write operations are tightly limited (e.g., 5 skill registrations per 10 minutes). Read operations are generous (60/min).

**Anomaly Detection** — Runs after every payment and execution. Detects self-payment, rapid repeat transactions, structuring near limits, and volume spikes. Advisory mode: flags are logged but transactions aren't blocked.

**Rating Integrity** — Ratings require a payment preimage (SHA-256 proof of purchase). One rating per execution (enforced by unique constraint). 30-second minimum delay. Weighted averages discount repeat reviewers (1/n diminishing weight).

**Provider Verification** — Providers can prove identity via Lightning node signatures (`lncli signmessage`) or domain verification (`.well-known` URL). Verified skills display trust badges in marketplace listings.

## Configuration

All settings via environment variables or `.env`:

```bash
# API Key (required)
CONDUIT_API_KEY=your-secret-key

# LND Connection
LND_HOST=192.168.1.x
LND_GRPC_PORT=10009
LND_TLS_CERT_PATH=credentials/full-chain.pem
LND_MACAROON_PATH=credentials/admin.macaroon
LND_NETWORK=mainnet

# Database
DATABASE_URL=postgresql+asyncpg://conduit:conduit@localhost:5432/conduit

# Spending Limits (sats, 0 = no limit)
SPENDING_LIMIT_PER_PAYMENT_SATS=10000
SPENDING_LIMIT_HOURLY_SATS=50000
SPENDING_LIMIT_DAILY_SATS=200000
SPENDING_CONFIRM_ABOVE_SATS=5000

# Keep false for MCP servers (echo corrupts stdio transport)
DEBUG=false
```

## Project Structure

```
src/conduit/
├── mcp_server.py                # MCP server entry point — 27 tools
├── core/
│   ├── config.py                # Settings from .env
│   └── database.py              # Async SQLAlchemy + asyncpg
├── models/
│   ├── skill.py                 # Skill marketplace listings
│   ├── execution.py             # Skill execution tracking
│   ├── rating.py                # Payment-proof-backed ratings
│   ├── spending_log.py          # Spending audit trail
│   └── anomaly_flag.py          # Suspicious pattern flags
├── services/
│   ├── lnd.py                   # LND gRPC client (sign, verify, pay)
│   ├── spending_limiter.py      # Payment limit enforcement
│   ├── macaroon_auth.py         # Scoped authorization tokens
│   ├── rate_limiter.py          # Sliding window rate limits
│   ├── anomaly_detector.py      # Transaction pattern detection
│   ├── rating_integrity.py      # Anti-gaming for ratings
│   ├── provider_verification.py # Node + domain verification
│   └── skill_executor.py        # Webhook-based execution engine
└── alembic/                     # Database migrations
```

## Roadmap

- [x] Lightning Network integration (LND gRPC)
- [x] MCP server with 27 tools (stdio + streamable-HTTP transports)
- [x] Skill marketplace (register, discover, execute, rate)
- [x] PostgreSQL persistence with Alembic migrations
- [x] Full security stack (auth, macaroons, limits, anomaly detection)
- [x] Provider verification (Lightning node + domain)
- [x] One-command install script
- [x] Nostr protocol for decentralized skill discovery (NIP-01/19/33)
- [x] Nostr Wallet Connect (NWC) with NIP-44 v2 encryption
- [x] REST API layer alongside MCP (31 endpoints, FastAPI)
- [x] Package for distribution (`pip install conduit-lightning`, `npx conduit-setup`)
- [x] Federation #1 — shared reputation layer: payer-bound rating attestations over Nostr, sybil-resistant aggregation, Postgres cache, opt-out publishing (`FEDERATION_ENABLED`)
- [x] Federation #1.5 — reputation peering: nodes serve and pull cached attestations directly from each other (peer-serve endpoint, peer-pull transport, background cache refresh), no longer relay-only
- [x] Federation #2 — node-to-node skill catalog sharing: verified remote skill listings (signed kind-38383 events, re-verified on ingest, self-excluded) pulled from relays + peers into a Postgres cache and merged into discovery — origin-tagged, remote verification badges neutralized, federated reputation overlay applied; peer-serve endpoint (`GET /api/v1/federation/skills`) + background refresh. Cross-node execution is still rejected (that's #3)
- [ ] Federation #3 — cross-node skill execution + payment routing

## License

MIT — see [LICENSE](LICENSE).
