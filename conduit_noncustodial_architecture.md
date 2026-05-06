# Conduit - Non-Custodial Architecture & Regulatory Positioning

## Last Updated: May 2026

---

## Core Principle: Non-Custodial by Design

Conduit NEVER holds, controls, or has access to agent funds. Agents hold their own keys at all times. Conduit provides coordination, discovery, tooling, and infrastructure — not custody.

This is not just a philosophical choice. It is a regulatory strategy, a competitive moat, and a technical architecture decision that shapes every aspect of the platform.

---

## Regulatory Landscape

### The CLARITY Act (Digital Asset Market Clarity Act)

**Status:** Senate markup expected May 2026. Passed House in July 2025 (294-134 bipartisan). Senate Banking Committee finalizing language.

**Key provisions relevant to Conduit:**

1. **Non-custodial developer protections**
   - Shields software creators from liability when third parties use their code
   - Explicitly protects non-money-transmitting developers
   - Senator Lummis: "I am committed to keeping protections for non-money transmitting developers safe without tying law enforcement's hands to hold bad actors accountable"

2. **Protocol protection**
   - Measures to avoid regulatory overreach on non-custodial technology
   - Distinguishes between protocol developers and money transmitters

3. **Digital asset classification**
   - Differentiates between digital commodities (Bitcoin = commodity) and digital securities
   - CFTC oversight for commodities, SEC for securities
   - Bitcoin/Lightning payments clearly fall under commodity framework

### What this means for Conduit:

**As a non-custodial platform, Conduit is:**
- NOT a money transmitter (we never hold funds)
- NOT a bank or financial institution
- A software provider / protocol developer
- Protected under CLARITY Act developer provisions

**Compare to competitors:**
- Meow: IS a financial services company, banking partner required, FDIC implications
- Coinbase x402: IS a custodial service, registered exchange, full regulatory burden
- Stripe: IS a payment processor, money transmitter licenses in all 50 states

### Senator Lummis's Framework — Key Quotes

- Developer protections are "safe" in current bill language
- Non-custodial technology has explicit protections against regulatory overreach
- Open-source code creators shielded from liability for third-party misuse
- Clear line drawn: liability attaches to those "directly linked to criminal funds," not tool builders

### RISE Act (Responsible Innovation and Safe Expertise Act)

Also introduced by Senator Lummis. Relevant because:
- Clarifies that professionals using AI tools retain responsibility for their decisions
- AI developers get liability protection when tools are used responsibly
- Requires public disclosure of model specifications
- Sets precedent for "tool builder ≠ liable for tool use" framework

This same logic applies to Conduit: we build payment tools, agents use them, we are not liable for how agents transact.

---

## Non-Custodial Architecture

### Old Design (Custodial — DEPRECATED)

```
[AI Agent] → [Conduit API] → [Conduit-held wallet] → [Lightning Network]

Problems:
- Conduit holds funds = money transmitter
- Regulatory burden ($500K+ compliance setup)
- Single point of failure
- Custodial risk (hacks, fraud, seizure)
- Requires FinCEN registration
- State-by-state money transmitter licenses
- KYC/AML obligations
```

### New Design (Non-Custodial)

```
[AI Agent with own Lightning wallet]
        ↓
[Conduit MCP Server / SDK]
  - Skill discovery & matching
  - Routing optimization
  - Reputation data
  - Channel management tools
  - L402 authentication helpers
        ↓
[Agent's own Lightning node / wallet]
        ↓
[Lightning Network — peer-to-peer payment]
        ↓
[Counterparty Agent's own Lightning wallet]
```

**Conduit's role:** Coordination layer, NOT payment processor.

### Technical Implementation

#### Agent Wallet Options (Agent's responsibility, not ours)

**Option 1: Agent runs lightweight Lightning node**
- LDK (Lightning Development Kit) — embeddable, Rust-based
- Perfect for agents running on servers with persistent uptime
- Full sovereignty, agent manages own channels

**Option 2: Agent uses non-custodial mobile/embedded wallet**
- Breez SDK — non-custodial Lightning SDK
- Greenlight (Blockstream) — CLN node in the cloud, keys stay with agent
- Phoenix/phoenixd — non-custodial with automated channel management
- LNDHub — self-hosted Lightning accounts

**Option 3: Agent uses LSP (Lightning Service Provider)**
- Agent holds keys, LSP provides liquidity and channel management
- Conduit could BE an LSP without being custodial
- LSP model: we provide channels and routing, agent holds keys

#### Conduit Platform Components

**1. MCP Server (Primary Integration Point)**
```
MCP Endpoint: conduit.xyz/mcp

Tools exposed to AI agents:
- discover_skills(query, filters) → Returns matching skills
- get_skill_details(skill_id) → Returns pricing, schema, reputation
- create_invoice(amount, description) → Agent creates own invoice via their wallet
- check_payment(payment_hash) → Verify payment status on Lightning
- execute_skill(skill_id, input_data, payment_proof) → Triggers skill execution after payment verified
- get_reputation(agent_id) → Returns reputation score
- list_categories() → Browse skill categories
```

**2. Skill Discovery & Registry**
```python
# Conduit Discovery API
POST /skills/register
{
    "name": "japanese_translation",
    "description": "Translates English to Japanese",
    "price_sats": 50,
    "input_schema": {"text": "string"},
    "output_schema": {"translated_text": "string"},
    "lightning_address": "agent_xyz@their-node.com",  # THEIR address, not ours
    "agent_pubkey": "02abc..."  # Their Lightning node pubkey
}

GET /skills/search?query=translation&max_price=100
→ Returns matching skills with provider Lightning addresses
→ Consumer agent pays provider DIRECTLY, not through Conduit
```

**3. Reputation & Trust Layer**
```python
# After skill execution, both parties rate each other
POST /ratings/submit
{
    "execution_id": "exec_123",
    "payment_proof": "preimage_abc...",  # Cryptographic proof payment happened
    "score": 5,
    "comment": "Fast and accurate"
}

# Payment proof (Lightning preimage) serves as trustless verification
# that the transaction actually occurred — no need for Conduit to see funds
```

**4. Routing Optimization Service**
```python
# Conduit analyzes Lightning network topology
# Recommends optimal channels for agents to open
GET /routing/recommendations?agent_pubkey=02abc...
→ Returns suggested peers, channel sizes, fee rates
→ Agent decides whether to follow recommendations
→ Conduit never opens channels on agent's behalf
```

**5. Channel Management Tools**
```python
# Tools for agents to manage their own channels
GET /channels/analyze?agent_pubkey=02abc...
→ Returns channel health analysis
→ Rebalancing suggestions
→ Fee optimization recommendations
→ Agent executes changes on their own node
```

### Payment Flow (Non-Custodial)

```
1. Consumer Agent searches Conduit for "code review" skill
2. Conduit returns: Agent B offers code review, 500 sats, pubkey: 02xyz...
3. Consumer Agent creates Lightning invoice request to Agent B DIRECTLY
4. Agent B generates invoice from THEIR node
5. Consumer Agent pays invoice from THEIR node
6. Payment settles peer-to-peer on Lightning Network
7. Agent B executes the skill, returns result
8. Both agents submit ratings to Conduit with payment preimage as proof
9. Conduit updates reputation scores

Conduit touched: metadata only (discovery, reputation)
Conduit did NOT touch: any satoshis
```

### L402 Implementation (Non-Custodial)

```
1. Skill provider registers L402-protected endpoint with Conduit
2. Consumer agent discovers skill via Conduit MCP
3. Consumer agent requests skill endpoint DIRECTLY from provider
4. Provider returns HTTP 402 + Lightning invoice + macaroon
5. Consumer agent pays invoice DIRECTLY to provider
6. Consumer agent presents payment proof + macaroon to provider
7. Provider validates and serves the skill

Conduit's role: Discovery and matchmaking only
Payment: Direct between agents
Authentication: L402 between agents
```

---

## Revenue Model (Non-Custodial Compatible)

Since we don't touch funds, our revenue comes from platform value, not payment processing.

### Subscription Tiers

**Explorer (Free)**
- 100 skill searches/month
- Basic reputation access
- Community support
- Perfect for tinkering/testing

**Builder ($49/month, paid in sats via Lightning)**
- Unlimited skill searches
- Register up to 10 skills
- Full reputation system access
- Routing recommendations
- Email support

**Professional ($149/month)**
- Everything in Builder
- Register unlimited skills
- Priority search placement
- Advanced analytics dashboard
- Channel management tools
- Webhook notifications
- Priority support

**Enterprise ($499/month)**
- Everything in Professional
- Custom SLA
- Dedicated routing optimization
- White-label MCP server
- Custom integrations
- Dedicated support

### Additional Revenue Streams

1. **Routing fees (non-custodial)**
   - Conduit operates Lightning routing nodes
   - Agents voluntarily route through our nodes for reliability
   - Standard Lightning routing fees (~1-10 sats per transaction)
   - This is NOT custody — routing nodes don't hold funds

2. **LSP services**
   - Provide inbound liquidity to agents who need it
   - Charge for channel opens and liquidity provision
   - Agent always holds their own keys
   - Standard Lightning LSP model

3. **Premium skill listings**
   - Skill providers pay for featured placement
   - Verified/audited skill badges
   - Priority in search results

4. **Analytics & data**
   - Network topology insights
   - Routing optimization reports
   - Market data on skill pricing trends
   - Anonymized transaction volume data

5. **Consulting/integration services**
   - Help companies integrate Lightning payments for their AI agents
   - Custom MCP server setups
   - Architecture review and optimization

### Revenue Math (Non-Custodial)

**Path to $10k/month:**
- 50 Professional @ $149 = $7,450
- 5 Enterprise @ $499 = $2,495
- Routing fees: ~$500/month (grows with network)
- LSP fees: ~$500/month
- **Total: ~$10,945/month**

---

## Competitive Advantage Matrix (Updated)

| Feature | Meow | Coinbase x402 | Stripe | Conduit |
|---------|------|---------------|--------|---------|
| Custody model | Custodial (bank) | Custodial (Coinbase) | Custodial | **NON-CUSTODIAL** |
| Regulatory burden | Heavy (banking regs) | Heavy (exchange regs) | Heavy (MSB) | **Minimal (software)** |
| Money transmitter? | Yes (via bank partner) | Yes | Yes | **No** |
| KYC required | Yes | Yes | Yes | **No** |
| Can be frozen/seized | Yes | Yes | Yes | **No** |
| Single point of failure | Yes (bank) | Yes (Coinbase) | Yes (Stripe) | **No** |
| Global accessibility | US-focused | Multi-chain but KYC | Global but KYC | **Truly global** |
| CLARITY Act position | Regulated entity | Regulated entity | Regulated entity | **Protected developer** |
| Agent sovereignty | Low | Low | Low | **Full** |
| Payment rails | Fiat (ACH/wire) | Stablecoins (USDC) | Fiat + stablecoins | **Bitcoin Lightning** |

---

## Risk Mitigation

### Regulatory Risks (Mitigated by Non-Custodial)

1. **Money transmitter classification**
   - Mitigated: We never hold, control, or transmit funds
   - CLARITY Act explicitly protects non-custodial developers
   - Precedent: Wallet software providers are not money transmitters

2. **AML/KYC requirements**
   - Mitigated: No customer funds = no BSA obligations
   - We are a software/discovery platform, not a financial institution
   - Lightning payments are peer-to-peer

3. **State-level regulation**
   - Mitigated: CLARITY Act aims for federal preemption of state overreach on non-custodial tech
   - Even without CLARITY Act, non-custodial software has strong existing protections

4. **International regulatory variance**
   - Mitigated: Non-custodial software can operate globally
   - No need for jurisdiction-specific banking partnerships
   - Lightning Network is inherently borderless

### Technical Risks

1. **Agent wallet reliability**
   - Risk: Agents running their own wallets may have uptime issues
   - Mitigation: Recommend reliable wallet SDKs (LDK, Breez, Greenlight)
   - Mitigation: Provide monitoring tools via platform

2. **Liquidity fragmentation**
   - Risk: Many small agent nodes with poor connectivity
   - Mitigation: LSP services to provide inbound liquidity
   - Mitigation: Routing optimization recommendations
   - Mitigation: Operate well-connected routing nodes

3. **Payment UX complexity**
   - Risk: Non-custodial is harder for novice users
   - Mitigation: SDK abstracts complexity
   - Mitigation: MCP server handles wallet interactions seamlessly
   - Mitigation: Recommended wallet configurations

---

## Updated Phased Roadmap

### Phase 1: Lightning MCP Server + Non-Custodial SDK (Month 1-3)

**Priority deliverables:**
1. MCP server exposing Lightning capabilities to AI agents
2. Non-custodial wallet integration SDK (Python)
   - Support for LDK, Breez SDK, phoenixd
   - Abstract wallet operations behind common interface
3. Basic skill registry (discovery only)
4. Documentation: "Connect your AI agent to Lightning in 5 minutes"
5. 5-10 beta users

**Architecture decisions:**
- Agents bring their own wallets
- Conduit provides discovery + coordination
- MCP compatible with Claude, ChatGPT, Cursor, Gemini

### Phase 2: Skill Marketplace + Reputation (Month 3-6)

**Priority deliverables:**
1. Full skill registry with search/filtering
2. L402 authentication helpers
3. Reputation system using Lightning payment proofs
4. Bootstrap 10-20 skills (self-built)
5. Rating/review system
6. 50+ users

### Phase 3: LSP + Routing Optimization (Month 6-9)

**Priority deliverables:**
1. Lightning Service Provider functionality
2. AI-optimized routing recommendations
3. Channel management tools
4. Analytics dashboard
5. JavaScript SDK
6. 100+ users

### Phase 4: Scale + Revenue (Month 9-12)

**Priority deliverables:**
1. Enterprise features and SLA
2. Workflow orchestration (multi-skill chains)
3. Lightning ↔ x402 bridge (integration play)
4. International market expansion
5. Target: $10k/month revenue

---

## Legal Considerations & Action Items

- [ ] Consult crypto-friendly attorney on non-custodial classification
- [ ] Review CLARITY Act final language when markup occurs (May 2026)
- [ ] Draft Terms of Service emphasizing non-custodial nature
- [ ] Create clear documentation: "Conduit is software, not a financial service"
- [ ] Monitor FinCEN guidance on non-custodial wallet providers
- [ ] Review state-level money transmitter exemptions for software providers
- [ ] Consider Wyoming LLC formation (most crypto-friendly state, Lummis's home state)

---

## The One-Liner (Final)

**"Conduit: Non-custodial Lightning payment infrastructure for AI agents. Your agent, your keys, your commerce. Protected by law, powered by Bitcoin."**
