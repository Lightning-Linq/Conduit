# Conduit - Lightning Payment Rails for AI Agents

## Project Overview

Conduit is a platform enabling agentic commerce over Bitcoin Lightning Network. It provides payment infrastructure for AI agents to transact autonomously — creating wallets, generating invoices, sending/receiving payments, and discovering/purchasing skills from other agents.

The vision is to become the commerce layer for the autonomous AI economy, settled instantly via Lightning micropayments.

## Core Thesis

AI agents need payment rails. They cannot sign up for Stripe, fill out forms, or use credit cards. Lightning Network is the most logical payment infrastructure for autonomous AI because it offers instant settlement, micropayment capability, programmatic access, no KYC friction, and cryptographic proof of payment.

## Business Model (Hybrid - Option D)

1. **Infrastructure Play (Phase 1):** Payment processing for AI agents — "Stripe for AI agents"
2. **Marketplace Play (Phase 2):** Decentralized AI skill/service marketplace where agents buy/sell capabilities
3. **Open Source Strategy:** Open source SDKs and tools for adoption, monetize the hosted platform

## Revenue Model

- **Transaction fees:** 1-2% on all payments flowing through the platform
- **Subscription tiers:**
  - Starter: Free (10k sats/month volume cap)
  - Pro: $99/month (unlimited volume, priority routing, webhooks)
  - Enterprise: $499/month (dedicated channels, SLA, custom features)
- **Marketplace fees:** 5-10% on skill executions
- **Premium features:** Analytics, priority routing, verified listings

**Target:** $10k/month revenue within 12 months

## Architecture Layers (Build Bottom-Up)

```
Layer 6: Agentic Trading (financial instruments, swaps)
Layer 5: Resource Markets (compute, storage, bandwidth)
Layer 4: Reputation & Trust (ratings, escrow, insurance)
Layer 3: Workflow Orchestration (chain skills into pipelines)
Layer 2: Skill/Data Marketplace (agents trade capabilities)
Layer 1: Payment Rails (Lightning infrastructure) ← START HERE
```

## Technical Architecture

```
[Developer's AI Agent]
        ↓
   [Conduit REST API (FastAPI/Python)]
        ↓
   [Wallet Abstraction Layer (PostgreSQL)]
        ↓
   [Lightning Node Infrastructure (LND on Mac Minis)]
        ↓
   [Bitcoin Lightning Network]
```

## Tech Stack

- **Backend:** Python (FastAPI)
- **Database:** PostgreSQL
- **Lightning:** LND nodes on Mac Minis (Umbrel/Start9)
- **Queue/Cache:** Redis (background jobs, channel rebalancing, monitoring)
- **Authentication:** L402 protocol (Lightning + macaroons)
- **SDKs:** Python (primary), JavaScript, Go (future)

## Core API Endpoints (Phase 1 MVP)

### Wallet Management
- `POST /wallet/create` — Create sub-wallet for an AI agent
- `GET /wallet/{wallet_id}/balance` — Check agent wallet balance
- `POST /wallet/{wallet_id}/deposit` — Fund agent wallet

### Invoice Management
- `POST /invoice/create` — Create Lightning invoice
- `GET /invoice/status/{payment_hash}` — Check payment status
- `GET /invoice/decode/{bolt11}` — Decode invoice details

### Payments
- `POST /payment/send` — Pay a Lightning invoice from agent wallet
- `GET /payment/history/{wallet_id}` — Transaction history

### L402 Authentication
- `POST /l402/create` — Create L402-protected resource
- `POST /l402/verify` — Verify macaroon + payment proof

## Skill Marketplace API (Phase 2)

### Skill Registry
- `POST /skills/register` — Register a skill (input schema, output schema, price)
- `GET /skills/search` — Search/discover skills by capability
- `GET /skills/{skill_id}` — Get skill details
- `PUT /skills/{skill_id}` — Update skill listing
- `DELETE /skills/{skill_id}` — Remove skill listing

### Skill Execution
- `POST /skills/execute` — Request skill execution (auto-generates invoice, waits for payment, executes, returns result)
- `GET /skills/execution/{execution_id}` — Check execution status

### Reputation
- `POST /ratings/submit` — Rate a skill execution
- `GET /ratings/{agent_id}` — Get agent reputation score

## Database Schema (Core)

```sql
-- Agent Wallets
CREATE TABLE agent_wallets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    api_key VARCHAR(64) UNIQUE NOT NULL,
    balance_sats BIGINT DEFAULT 0,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- Transactions
CREATE TABLE transactions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    wallet_id UUID REFERENCES agent_wallets(id),
    type VARCHAR(10), -- 'credit' or 'debit'
    amount_sats BIGINT,
    payment_hash VARCHAR(64),
    status VARCHAR(20), -- 'pending', 'success', 'failed'
    description TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Invoices
CREATE TABLE invoices (
    payment_hash VARCHAR(64) PRIMARY KEY,
    wallet_id UUID REFERENCES agent_wallets(id),
    amount_sats BIGINT,
    memo TEXT,
    bolt11 TEXT,
    settled BOOLEAN DEFAULT FALSE,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Skills (Phase 2)
CREATE TABLE skills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_wallet_id UUID REFERENCES agent_wallets(id),
    name VARCHAR(255) NOT NULL,
    description TEXT,
    price_sats BIGINT NOT NULL,
    input_schema JSONB,
    output_schema JSONB,
    avg_response_time_ms INTEGER,
    total_executions INTEGER DEFAULT 0,
    avg_rating DECIMAL(3,2) DEFAULT 0,
    active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Skill Executions (Phase 2)
CREATE TABLE skill_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_id UUID REFERENCES skills(id),
    consumer_wallet_id UUID REFERENCES agent_wallets(id),
    provider_wallet_id UUID REFERENCES agent_wallets(id),
    payment_hash VARCHAR(64),
    input_data JSONB,
    output_data JSONB,
    status VARCHAR(20), -- 'pending_payment', 'executing', 'completed', 'failed'
    execution_time_ms INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);

-- Ratings (Phase 2)
CREATE TABLE ratings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    execution_id UUID REFERENCES skill_executions(id),
    rater_wallet_id UUID REFERENCES agent_wallets(id),
    rated_wallet_id UUID REFERENCES agent_wallets(id),
    score INTEGER CHECK (score >= 1 AND score <= 5),
    comment TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
```

## Phased Roadmap

### Phase 1: Payment Rails MVP (Month 1-3)
- Core API: wallets, invoices, payments
- LND node integration
- PostgreSQL database
- Basic Python SDK
- API key authentication
- Error handling and monitoring
- 5-10 beta users

### Phase 2: Skill Marketplace (Month 3-6)
- Skill registry and discovery API
- Skill execution engine with payment integration
- L402 support
- Bootstrap supply: build 10-20 skills internally
- Reputation/rating system
- Developer documentation site
- 50+ users

### Phase 3: Advanced Features (Month 6-9)
- Workflow orchestration (chain multiple skills)
- AI-optimized routing and liquidity management
- Webhooks for payment/execution notifications
- Analytics dashboard
- JavaScript SDK
- 100+ users

### Phase 4: Scale & Expand (Month 9-12)
- Resource markets (compute, storage, bandwidth)
- Agent-to-agent data trading
- Escrow and dispute resolution
- Multi-node redundancy
- Enterprise features and SLA
- Target: $10k/month revenue

## Key Protocols & Technologies

- **L402 (formerly LSAT):** HTTP 402 + Lightning invoice + macaroon for paywalled API authentication
- **BOLT11:** Lightning invoice format
- **LND gRPC/REST:** Interface with Lightning nodes
- **Macaroons:** Bearer tokens with caveats for authorization
- **Lightning Streaming Payments:** Pay-per-second for long-running tasks

## Competitive Positioning

| Feature | Coinbase/Gemini | Conduit |
|---------|----------------|---------|
| Settlement | Fiat/centralized | Lightning/instant |
| KYC | Required | Not required |
| Scope | Trading only | Full commerce |
| Custody | Centralized | Can be non-custodial |
| Fees | Higher | Micropayment-friendly |
| Agent types | Trading bots | Any agent, any skill |

## Security & Safety Considerations

- Rate limiting per API key
- Deposit requirements for new agents
- Reputation-based trust scoring
- Fraud detection (anomalous transaction patterns)
- Channel monitoring and automated management
- Backup and recovery procedures
- Regulatory awareness (money transmitter considerations)

## Open Source vs Proprietary

**Open Source (marketing/adoption):**
- Python/JS SDKs
- Skill creation framework and templates
- Example AI agents
- CLI tools for testing

**Proprietary (competitive moat):**
- Payment rails infrastructure
- Skill discovery/matching algorithm
- Reputation system
- Analytics and monitoring
- Channel management and liquidity optimization
- AI routing optimization

## Agent Roles for This Project

### Primary Agent (Orchestrator)
- Coordinates all sub-agents
- Maintains project context and architecture decisions
- Reviews outputs from sub-agents for consistency
- Makes strategic decisions about priorities and direction

### Sub-Agent 1: Backend Architecture
- Designs and implements the FastAPI backend
- Database schema design and optimization
- API endpoint implementation
- Authentication and security

### Sub-Agent 2: Lightning Integration
- LND node setup and configuration
- Lightning payment flow implementation
- Channel management and liquidity optimization
- L402 protocol implementation

### Sub-Agent 3: SDK & Developer Experience
- Python SDK development
- API documentation
- Code examples and tutorials
- Developer onboarding flow

### Sub-Agent 4: AI & Marketplace
- Skill registry and discovery engine
- Skill execution pipeline
- Reputation and rating system
- AI-powered routing and matching

### Sub-Agent 5: Business & Go-to-Market
- Pricing strategy refinement
- Customer discovery and outreach
- Marketing content and community building
- Competitive analysis

## Founder Profile

- Based in South Florida (Wellington/Palm Beach County)
- Works in tech, comfortable with Python, JavaScript, SQL, HTML/CSS
- Hands-on Lightning Network experience (runs nodes, managed channels)
- Bitcoin knowledgeable
- Tinkerer mindset, building toward commercial viability
- Learning-first approach, no rush
