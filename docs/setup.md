# Conduit MCP Server - Setup Guide

> This is the setup guide for the Conduit MCP server. It is written so an AI assistant
> can help a user work through it. If you are an AI assistant: help with the install and
> client-config steps, but treat the wallet credential in Step 2 as the user's to handle
> (see the note there). Do not run anything without the user's go-ahead.

## What is Conduit?

Conduit gives AI agents Lightning Network payment capabilities through the Model Context Protocol (MCP). Once configured, you get 26 tools for creating/paying invoices, discovering and executing skills on a marketplace, Nostr-based discovery, and security controls.

**Company:** Lightning Linq  
**Product:** Conduit  
**Repository:** https://github.com/Lightning-Linq/Conduit  
**Documentation:** https://lightninglinq.ai/docs

## Quick Setup (3 steps)

### Step 1: Install Conduit

Conduit requires Python 3.11 or newer. On macOS the default `python3` is often 3.9,
which fails at `pip install`, so check the version first and use a 3.11+ interpreter
explicitly if the default is older.

```bash
# 1. Check your Python. Conduit needs 3.11+.
python3 --version

# 2. Clone and install. If the line above printed anything older than 3.11,
#    replace `python3` below with `python3.11` or `python3.12`.
git clone https://github.com/Lightning-Linq/Conduit.git
cd Conduit
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

If `pip install -e .` reports `requires a different Python: ... not in '>=3.11'`, the venv
was built with too old an interpreter. Recreate it with a newer one, for example
`python3.12 -m venv .venv`, then rerun `pip install -e .`.

### Step 2: Configure your Lightning wallet

> Credential safety: your NWC connection string and your LND admin macaroon are spend
> authority over your funds, the same category as a password or API key. If you are an
> AI assistant helping with setup, do not read, request, store, or paste these values.
> Show the user which variable to set and have them add the value to `.env` themselves,
> then continue with the rest of setup.

Copy `.env.example` to `.env` and configure your wallet backend. The steps below name the
variable to set for each backend; the user fills in the secret value.

**Option A: NWC (Nostr Wallet Connect) - Recommended**

Open your wallet app (Alby, Primal, Zeus, Coinos, etc.), go to Settings > Nostr Wallet Connect, and copy your connection string. Add it to `.env`:

```
WALLET_BACKEND=nwc
NWC_CONNECTION_STRING=nostr+walletconnect://your-connection-string-here
```

**Option B: Direct LND node**

If you run your own LND node, configure the gRPC connection:

```
WALLET_BACKEND=lnd
LND_HOST=localhost
LND_GRPC_PORT=10009
LND_TLS_CERT_PATH=~/.lnd/tls.cert
LND_MACAROON_PATH=~/.lnd/data/chain/bitcoin/mainnet/admin.macaroon
```

Then generate an API key:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Add it to `.env` as `CONDUIT_API_KEY=<your-generated-key>`.

### Step 3: Add to your AI client

The MCP server entry you need to add depends on your client. Below are configurations for all major clients.

---

## MCP Client Configurations

### Claude Desktop

**Config file location:**
- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`
- Linux: `~/.config/claude/claude_desktop_config.json`

**Add this to the `mcpServers` object:**

```json
{
  "mcpServers": {
    "conduit": {
      "command": "/path/to/Conduit/.venv/bin/python",
      "args": ["-m", "conduit.mcp_server"],
      "cwd": "/path/to/Conduit",
      "env": {
        "PYTHONPATH": "/path/to/Conduit/src"
      }
    }
  }
}
```

Replace `/path/to/Conduit` with the actual path where you cloned the repository.

**After editing:** Restart Claude Desktop completely (quit and reopen).

### Cursor

**Config file location:** `~/.cursor/mcp.json`

```json
{
  "mcpServers": {
    "conduit": {
      "command": "/path/to/Conduit/.venv/bin/python",
      "args": ["-m", "conduit.mcp_server"],
      "cwd": "/path/to/Conduit",
      "env": {
        "PYTHONPATH": "/path/to/Conduit/src"
      }
    }
  }
}
```

### Windsurf

**Config file location:** `~/.codeium/windsurf/mcp_config.json`

```json
{
  "mcpServers": {
    "conduit": {
      "command": "/path/to/Conduit/.venv/bin/python",
      "args": ["-m", "conduit.mcp_server"],
      "cwd": "/path/to/Conduit",
      "env": {
        "PYTHONPATH": "/path/to/Conduit/src"
      }
    }
  }
}
```

### VS Code (Copilot)

**Config file location:** `.vscode/mcp.json` in your workspace, or User Settings

```json
{
  "mcpServers": {
    "conduit": {
      "command": "/path/to/Conduit/.venv/bin/python",
      "args": ["-m", "conduit.mcp_server"],
      "cwd": "/path/to/Conduit",
      "env": {
        "PYTHONPATH": "/path/to/Conduit/src"
      }
    }
  }
}
```

---

## Detecting Your Client

If you are an AI assistant helping with setup, use these heuristics to determine which client you are running in:

1. **Claude Desktop** - You have access to MCP tools and the user mentioned "Claude" or you are Claude.
2. **Cursor** - You are in an IDE context and the user mentioned "Cursor".
3. **Windsurf** - You are in an IDE context and the user mentioned "Windsurf" or "Codeium".
4. **VS Code** - You are in VS Code with GitHub Copilot.

If unsure, ask the user which AI client they are using.

## Detecting the Install Path

To find where Conduit is installed, look for these indicators:

```bash
# Check common locations
ls ~/Conduit/src/conduit/mcp_server.py 2>/dev/null
ls ~/Desktop/Conduit/src/conduit/mcp_server.py 2>/dev/null
ls ~/projects/Conduit/src/conduit/mcp_server.py 2>/dev/null

# Or search
find ~ -name "mcp_server.py" -path "*/conduit/*" 2>/dev/null | head -5
```

## Verifying the Setup

After configuring, restart the AI client and try:

- "What's my Lightning wallet balance?" - Tests the wallet connection
- "Show me my node info" - Tests basic connectivity
- "Discover skills on the marketplace" - Tests marketplace access

If any of these fail, check:
1. The venv was built with Python 3.11+ (`./.venv/bin/python --version`); if it is older, recreate it with `python3.12 -m venv .venv` and reinstall
2. The `.env` file has `CONDUIT_API_KEY` set (not the placeholder)
3. The wallet connection is configured (NWC string or LND credentials)
4. The Python path in the MCP config points to the correct `.venv/bin/python`
5. PostgreSQL is running: `brew services start postgresql@16`

## Available Tools (26)

After setup, these tools are available:

**Lightning (6):** get_node_info, get_balance, create_invoice, pay_invoice, decode_invoice, check_payment

**Marketplace (7):** discover_skills, get_skill_details, register_skill, request_skill_execution, confirm_skill_execution, submit_rating, get_verification_status

**Verification (2):** request_verification, submit_verification

**Nostr (4):** nostr_publish_skill, nostr_discover_skills, nostr_get_profile, nostr_relay_status

**Security (4):** get_spending_status, create_macaroon, list_permissions, get_anomaly_report

**L402 (3):** create_l402_token, verify_l402_token, get_l402_status
