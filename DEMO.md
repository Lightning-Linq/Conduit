# Conduit End-to-End Demo

Two AI agents transacting over the Lightning Network: register a skill, discover it, execute it, pay for it, and rate it.

## Prerequisites

- **Conduit** installed and configured (see [Installation](https://lightninglinq.ai/docs.html#install))
- **PostgreSQL** running with the `conduit` database initialized (`alembic upgrade head`)
- **LND node** running and synced to chain, with at least one open channel
- **Python 3.11+** with the virtual environment activated

For full mode (real payments), you also need a **second Lightning wallet**: any wallet that can pay BOLT-11 invoices (Phoenix, Zeus, Alby, or another LND node).

## Quick Start

```bash
cd ~/conduit
source .venv/bin/activate

# Start the Conduit server
python -m conduit &

# Run the demo (local mode, free skill, single node)
python demo_e2e.py --mode local
```

## Demo Modes

### Local Mode (default)

Single-node demo using a free skill. Exercises the full marketplace protocol without requiring a second wallet or routing payments.

```bash
python demo_e2e.py --mode local
```

**What it does:**

1. **Health check**: verifies Conduit can talk to your LND node
2. **Register**: Provider agent registers a free "Sentiment Analyzer" skill
3. **Discover**: Consumer agent searches the marketplace and finds it
4. **Execute**: Consumer requests execution (completes immediately since free)
5. **Lightning demo**: Creates and decodes a test invoice to prove LND connectivity
6. **Rate**: Consumer rates the skill 5/5 (waits 30s for anti-gaming cooldown)
7. **Security check**: Displays active spending limits

**Expected output:**

```
Step 0  ✓ Connected to node: YourNode (03ab12...)
Step 1  ✓ Skill registered: Sentiment Analyzer (0 sats)
Step 2  ✓ Found skill(s) on marketplace
Step 3  ✓ Execution created (status: completed)
Step 4  ✓ Invoice created and decoded
Step 5  ✓ Rating submitted (5/5)
Step 6  ✓ Spending limits active
```

### Full Mode (real payments)

Two-node demo with real Lightning payments. A paid skill generates invoices that must be paid from an external wallet before the execution is confirmed.

```bash
python demo_e2e.py --mode full
python demo_e2e.py --mode full --price 100  # custom price (default: 50 sats)
```

**What it does:**

1. **Health check**: verifies LND connectivity
2. **Register**: Provider registers "Sentiment Analyzer Pro" at 50 sats
3. **Discover**: Consumer finds the paid skill
4. **Execute**: Consumer requests execution, receives two BOLT-11 invoices that together add up to exactly the listed price:
   - Skill invoice (49 sats → provider)
   - Platform fee invoice (1 sat → Conduit, 1.5% carved out of the price)
5. **Pay**: Copy the invoices into your second wallet and pay them. The demo polls every 5 seconds until both settle.
6. **Confirm**: Conduit verifies both invoices settled on LND, marks execution complete
7. **Rate**: Consumer submits rating with the real payment preimage as cryptographic proof (waits 30s cooldown)

**Expected output:**

```
Step 3  ✓ Execution created
        Listed price: 50 sats
        Provider receives: 49 sats
        Platform fee: 1 sats (carved out of the price)
        Buyer pays: 50 sats (== the listed price)
        Invoices: 2 (skill + platform fee)

Step 4  Pay this invoice from your SECOND node:
        lnbc500n1p...

        Then pay the platform fee invoice:
        lnbc10n1p...

        ✓ Skill invoice settled!
        Preimage: 09d0e6d2...
        ✓ Fee invoice settled!

Step 5  ✓ Execution confirmed (fee_settled: True)
Step 6  ✓ Rating submitted (5/5, weighted_average: 5.0)

Demo Complete!
  Provider listed at: 50 sats, earned: 49 sats (price minus the platform fee)
  Consumer spent: 50 sats (exactly the listed price)
  Platform fee: 1 sat (paid by the seller)
  Non-custodial: two separate invoices, Conduit never held funds
```

## Fees

Conduit is **fee-inclusive and seller-pays**: the buyer pays exactly the listed price,
and the platform fee is carved *out* of that price rather than added on top. The
provider's invoice is for `price - fee` and a second invoice covers the fee, so the two
always sum to the listed price and Conduit never receives-then-forwards a payment.

| Listed price | Provider receives | Platform fee | Buyer pays |
|---|---|---|---|
| 50 sats | 49 | 1 | 50 |
| 1,000 sats | 985 | 15 | 1,000 |
| 5 sats | 5 | 0 (waived) | 5 |

The rate is `TRANSACTION_FEE_PERCENT` (default 1.5), rounded up, with a 1 sat minimum.
Prices below `PLATFORM_FEE_WAIVE_BELOW_SATS` (default 10) carry no fee at all, so a
dust-priced skill is never eaten by rounding, those executions produce a single
invoice instead of two. The fee is also capped so the provider always nets at least
1 sat.

## CLI Options

| Flag | Default | Description |
|------|---------|-------------|
| `--mode` | `local` | `local` (free skill, single node) or `full` (paid, two nodes) |
| `--base-url` | `http://127.0.0.1:8000` | Conduit API server URL |
| `--price` | `50` | Skill price in sats (full mode only) |

## How It Works

The demo script uses Conduit's REST API to simulate two AI agents:

- **AgentSmith** (Provider): registers and offers skills
- **AgentNeo** (Consumer): discovers, pays for, and rates skills

In production, these would be separate AI agents (e.g., Claude instances) using Conduit's MCP tools. The demo uses HTTP requests to show the same flow.

### Payment flow (full mode)

```
Consumer                    Conduit                     Provider's LND
   |                          |                              |
   |-- request_execution ---->|                              |
   |<-- invoice (49 sats) ----|   provider's share           |
   |<-- fee invoice (1 sat) --|   50 listed = 49 + 1         |
   |                          |                              |
   |== pay invoice via LN ===========================>|      |
   |== pay fee invoice ====>|                              |
   |                          |                              |
   |-- confirm_execution ---->|                              |
   |                          |-- verify settled (LND) ----->|
   |                          |<-- settled: true ------------|
   |<-- execution complete ---|                              |
   |                          |                              |
   |-- submit_rating -------->|                              |
   |   (with preimage proof)  |                              |
   |<-- rating: 5/5 ----------|                              |
```

### Rating integrity

Ratings require cryptographic proof of payment:

- **Paid skills**: `SHA256(preimage) == payment_hash`, only someone who actually paid can rate
- **Free skills**: preimage check is skipped (no payment was made)
- **Anti-gaming**: 30-second cooldown after execution before rating is allowed
- **Sybil resistance**: weighted ratings discount repeat reviewers (1st = 1.0, 2nd = 0.5, 3rd = 0.33)

## Troubleshooting

### "Cannot reach Conduit at http://127.0.0.1:8000"

The server isn't running. Start it:

```bash
source .venv/bin/activate
python -m conduit
```

### "LND connection failed" or gRPC timeout

Your LND node isn't reachable. Check:

- Is the node running and synced?
- If connecting via Tor: is `tor` running and `socat` tunnel active?
- Try increasing the gRPC timeout (default: 30s)

For Tor connections (e.g., Start9 over .onion):

```bash
# Start Tor
brew services start tor  # macOS
sudo systemctl start tor  # Linux

# Start socat tunnel (replace YOUR_ONION_ADDRESS)
socat TCP-LISTEN:10009,fork,reuseaddr \
  "SOCKS4A:127.0.0.1:YOUR_ONION_ADDRESS.onion:10009,socksport=9050"
```

### "Port 8000 already in use"

Kill the old server process:

```bash
lsof -ti:8000 | xargs kill -9
```

### "self-payments not allowed" (full mode)

LND cannot pay its own invoices. You need a second wallet/node to pay the invoices. Use `--mode local` if you only have one node.

### Rating fails with HTTP 400

- **"Please wait N more seconds"**: the 30-second anti-gaming cooldown hasn't elapsed yet. The demo handles this automatically.
- **"Payment preimage does not match"**: for paid skills, the preimage must match `SHA256(preimage) == payment_hash`. The demo captures this from the settled invoice automatically.

### Redis warnings in server logs

```
WARNING: Redis unavailable, falling back to in-memory rate limiting
```

This is safe for development. Rate limiting works in-memory. For production, start Redis:

```bash
brew services start redis  # macOS
sudo systemctl start redis  # Linux
```
