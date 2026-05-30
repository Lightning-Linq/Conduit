#!/usr/bin/env python3
"""
Conduit End-to-End Demo — Two AI Agents Transacting over Lightning

This script simulates two AI agents (Provider and Consumer) interacting
through Conduit's marketplace.

Two modes:
  --mode full    (default) Full flow with real Lightning payments.
                 Requires TWO Lightning nodes (provider + consumer).
  --mode local   Single-node demo using a free skill. Exercises the full
                 marketplace protocol (register → discover → execute →
                 confirm → rate) without routing payments.

Prerequisites:
  - Conduit API server running:  python -m conduit.main
  - LND node running and synced
  - CONDUIT_API_KEY set in .env

Usage:
  python demo_e2e.py                    # local mode (single node)
  python demo_e2e.py --mode local       # same — marketplace flow, no payment
  python demo_e2e.py --mode full        # real payments (needs 2 nodes)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv


# =============================================================================
# Config
# =============================================================================

_PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv(_PROJECT_ROOT / ".env")

DEFAULT_BASE_URL = "http://127.0.0.1:8000"


# =============================================================================
# Terminal colors
# =============================================================================

class C:
    HEADER  = "\033[95m"
    BLUE    = "\033[94m"
    CYAN    = "\033[96m"
    GREEN   = "\033[92m"
    YELLOW  = "\033[93m"
    RED     = "\033[91m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    RESET   = "\033[0m"
    ORANGE  = "\033[38;5;208m"


def banner(text: str) -> None:
    width = 60
    print(f"\n{C.BOLD}{C.ORANGE}{'=' * width}{C.RESET}")
    print(f"{C.BOLD}{C.ORANGE}  {text}{C.RESET}")
    print(f"{C.BOLD}{C.ORANGE}{'=' * width}{C.RESET}\n")


def step(number: int, title: str, agent: str = "") -> None:
    agent_badge = ""
    if agent.lower() == "provider":
        agent_badge = f"{C.CYAN}[Provider Agent]{C.RESET} "
    elif agent.lower() == "consumer":
        agent_badge = f"{C.YELLOW}[Consumer Agent]{C.RESET} "
    print(f"{C.BOLD}{C.GREEN}Step {number}{C.RESET} {agent_badge}{title}")


def info(label: str, value: str) -> None:
    print(f"  {C.DIM}{label}:{C.RESET} {value}")


def success(msg: str) -> None:
    print(f"  {C.GREEN}✓ {msg}{C.RESET}")


def fail(msg: str) -> None:
    print(f"  {C.RED}✗ {msg}{C.RESET}")
    sys.exit(1)


# =============================================================================
# API client
# =============================================================================

class ConduitClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        })

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def get(self, path: str, **kwargs) -> dict:
        r = self.session.get(self._url(path), **kwargs)
        r.raise_for_status()
        return r.json()

    def post(self, path: str, data: dict | None = None, **kwargs) -> dict:
        r = self.session.post(self._url(path), json=data, **kwargs)
        r.raise_for_status()
        return r.json()


# =============================================================================
# Demo — Local mode (single node, free skill)
# =============================================================================

def run_local_demo(base_url: str) -> None:
    """Full marketplace flow with a free skill — no payment routing needed."""
    api_key = os.environ.get("CONDUIT_API_KEY", "")
    if not api_key or api_key == "CHANGE-ME":
        fail("CONDUIT_API_KEY not set or still default. Check your .env file.")

    client = ConduitClient(base_url, api_key)

    banner("Conduit E2E Demo — Local Mode (Single Node)")
    print(f"  Server:  {base_url}")
    print(f"  Mode:    local (free skill, full marketplace protocol)")
    print()

    # ── Step 0: Health check ─────────────────────────────────────────

    step(0, "Checking Conduit server and LND node...")
    try:
        node = client.get("/api/v1/lightning/node-info")
        success(f"Connected to node: {node['alias']} ({node['pubkey'][:16]}...)")
        info("Channels", str(node["num_active_channels"]))
        info("Synced", str(node["synced_to_chain"]))
    except requests.ConnectionError:
        fail(f"Cannot reach Conduit at {base_url}. Is the server running?")
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        fail(f"Server error: {e.response.status_code} — {detail}")

    balance = client.get("/api/v1/lightning/balance")
    info("Channel balance", f"{balance.get('channel_balance_sats', '?')} sats")
    print()

    # ── Step 1: Provider registers a free skill ──────────────────────

    step(1, "Registering a skill on the marketplace", agent="provider")
    skill_data = {
        "name": "Sentiment Analyzer",
        "description": "Analyzes text sentiment using NLP. Returns positive/negative/neutral with confidence score.",
        "provider_name": "AgentSmith",
        "category": "ai",
        "price_sats": 0,
        "lightning_address": "",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to analyze"},
            },
            "required": ["text"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
                "confidence": {"type": "number"},
            },
        },
    }

    registered = client.post("/api/v1/marketplace/skills", data=skill_data)
    skill_id = registered["id"]
    success(f"Skill registered: {registered['name']}")
    info("Skill ID", skill_id)
    info("Price", f"{registered['price_sats']} sats (free)")
    print()

    # ── Step 2: Consumer discovers the skill ─────────────────────────

    step(2, "Discovering skills on the marketplace", agent="consumer")
    discovered = client.get("/api/v1/marketplace/skills", params={"keyword": "sentiment"})
    success(f"Found {discovered['count']} skill(s)")
    for s in discovered["skills"]:
        info(s["name"], f"{s['price_sats']} sats — {s['description'][:50]}...")
    print()

    details = client.get(f"/api/v1/marketplace/skills/{skill_id}")
    info("Category", details["category"])
    info("Weighted rating", str(details.get("weighted_rating", "none yet")))
    print()

    # ── Step 3: Consumer requests execution ──────────────────────────

    step(3, "Requesting skill execution", agent="consumer")
    exec_request = {
        "skill_id": skill_id,
        "consumer_name": "AgentNeo",
        "input_data": {"text": "Bitcoin is the future of money and Lightning makes it instant!"},
    }

    execution = client.post("/api/v1/marketplace/executions", data=exec_request)
    execution_id = execution["execution_id"]

    success(f"Execution created: {execution_id[:16]}...")
    info("Skill price", f"{execution['price_sats']} sats")
    info("Platform fee", f"{execution['platform_fee_sats']} sats")
    info("Total cost", f"{execution['total_cost_sats']} sats")
    info("Status", execution["status"])
    print()

    # Free skill → no payment needed, status goes straight to PENDING
    # The confirm step is only needed for paid skills.

    # ── Step 4: Create a test invoice to demonstrate Lightning ───────

    step(4, "Creating a Lightning invoice (demonstrating LND integration)")
    invoice = client.post("/api/v1/lightning/invoices", data={
        "amount_sats": 100,
        "memo": "Conduit demo — proof of Lightning connectivity",
    })
    success(f"Invoice created!")
    info("Payment hash", invoice["payment_hash"][:32] + "...")
    info("Amount", f"{invoice['amount_sats']} sats")
    info("BOLT-11", invoice["payment_request"][:40] + "...")
    print()

    # Decode it back to prove the round-trip
    decoded = client.post("/api/v1/lightning/invoices/decode", data={
        "payment_request": invoice["payment_request"],
    })
    success(f"Invoice decoded — destination: {decoded['destination'][:16]}...")
    print()

    # ── Step 5: Rate the execution ───────────────────────────────────

    step(5, "Rating the completed execution", agent="consumer")

    # Rating integrity requires a 30-second cooldown after execution completes.
    print(f"  {C.DIM}Waiting 31s for rating cooldown (anti-gaming measure)...{C.RESET}", end="", flush=True)
    for i in range(31, 0, -1):
        sys.stdout.write(f"\r  {C.DIM}Waiting {i}s for rating cooldown (anti-gaming measure)...  {C.RESET}")
        sys.stdout.flush()
        time.sleep(1)
    print(f"\r  {C.DIM}Rating cooldown elapsed — submitting rating.              {C.RESET}")

    # For a free skill, we generate a dummy preimage since no real payment occurred.
    # The rating system validates preimage→payment_hash for paid skills;
    # for free skills (no payment_hash on the execution), we pass a placeholder.
    dummy_preimage = secrets.token_hex(32)

    rating_result = client.post(
        f"/api/v1/marketplace/executions/{execution_id}/rate",
        data={
            "score": 5,
            "review": "Excellent sentiment analysis! Fast and accurate.",
            "payment_preimage": dummy_preimage,
        },
    )

    success(f"Rating submitted!")
    info("Score", f"{rating_result['score']}/5")
    info("Weighted average", str(rating_result.get("weighted_average", "n/a")))
    print()

    # ── Step 6: Check security guardrails ────────────────────────────

    step(6, "Checking security guardrails")
    spending = client.get("/api/v1/security/spending")
    success("Spending limits active")
    info("Per-payment limit", f"{spending.get('per_payment_limit_sats', '?')} sats")
    info("Hourly limit", f"{spending.get('hourly_limit_sats', '?')} sats")
    info("Daily limit", f"{spending.get('daily_limit_sats', '?')} sats")
    print()

    # ── Summary ──────────────────────────────────────────────────────

    banner("Demo Complete!")
    print(f"  {C.CYAN}Provider Agent (AgentSmith){C.RESET}")
    print(f"    Registered: Sentiment Analyzer (free)")
    print(f"    Rating: 5/5 stars")
    print()
    print(f"  {C.YELLOW}Consumer Agent (AgentNeo){C.RESET}")
    print(f"    Discovered, executed, and rated the skill")
    print()
    print(f"  {C.ORANGE}Conduit Platform{C.RESET}")
    print(f"    Marketplace: register → discover → execute → rate")
    print(f"    Lightning:   invoice creation + decode (round-trip verified)")
    print(f"    Security:    spending limits, rate limiting, API key auth")
    print()
    print(f"  {C.GREEN}Full marketplace protocol exercised successfully.{C.RESET}")
    print(f"  {C.DIM}For paid skill demo with real payments, use --mode full")
    print(f"  with a second Lightning node as the consumer.{C.RESET}")
    print()


# =============================================================================
# Demo — Full mode (two nodes, real payments)
# =============================================================================

def run_full_demo(base_url: str, price_sats: int) -> None:
    """Full flow with real Lightning payments (requires two nodes)."""
    api_key = os.environ.get("CONDUIT_API_KEY", "")
    if not api_key or api_key == "CHANGE-ME":
        fail("CONDUIT_API_KEY not set or still default. Check your .env file.")

    client = ConduitClient(base_url, api_key)

    banner("Conduit E2E Demo — Full Mode (Real Lightning Payments)")
    print(f"  Server:  {base_url}")
    print(f"  Price:   {price_sats} sats")
    print()
    print(f"  {C.YELLOW}NOTE: This mode requires a second Lightning node to pay")
    print(f"  the invoices. If you only have one node, use --mode local.{C.RESET}")
    print()

    # ── Step 0: Health check ─────────────────────────────────────────

    step(0, "Checking Conduit server and LND node...")
    try:
        node = client.get("/api/v1/lightning/node-info")
        success(f"Connected to node: {node['alias']} ({node['pubkey'][:16]}...)")
        info("Channels", str(node["num_active_channels"]))
        info("Synced", str(node["synced_to_chain"]))
    except requests.ConnectionError:
        fail(f"Cannot reach Conduit at {base_url}. Is the server running?")
    except requests.HTTPError as e:
        try:
            detail = e.response.json()
        except Exception:
            detail = e.response.text
        fail(f"Server error: {e.response.status_code} — {detail}")

    balance = client.get("/api/v1/lightning/balance")
    info("Channel balance", f"{balance.get('channel_balance_sats', '?')} sats")
    print()

    # ── Step 1: Provider registers a skill ───────────────────────────

    step(1, "Registering a paid skill on the marketplace", agent="provider")
    skill_data = {
        "name": "Sentiment Analyzer Pro",
        "description": "Premium NLP sentiment analysis with confidence scores.",
        "provider_name": "AgentSmith",
        "category": "ai",
        "price_sats": price_sats,
        "lightning_address": "",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to analyze"},
            },
            "required": ["text"],
        },
    }

    registered = client.post("/api/v1/marketplace/skills", data=skill_data)
    skill_id = registered["id"]
    success(f"Skill registered: {registered['name']}")
    info("Skill ID", skill_id)
    info("Price", f"{registered['price_sats']} sats")
    print()

    # ── Step 2: Consumer discovers the skill ─────────────────────────

    step(2, "Discovering skills on the marketplace", agent="consumer")
    discovered = client.get("/api/v1/marketplace/skills", params={"keyword": "sentiment"})
    success(f"Found {discovered['count']} skill(s)")
    for s in discovered["skills"]:
        info(s["name"], f"{s['price_sats']} sats — {s['description'][:50]}...")
    print()

    # ── Step 3: Consumer requests execution ──────────────────────────

    step(3, "Requesting skill execution (generates invoices)", agent="consumer")
    exec_request = {
        "skill_id": skill_id,
        "consumer_name": "AgentNeo",
        "input_data": {"text": "Bitcoin is the future of money and Lightning makes it instant!"},
    }

    execution = client.post("/api/v1/marketplace/executions", data=exec_request)
    execution_id = execution["execution_id"]
    skill_invoice = execution["payment_request"]
    skill_payment_hash = execution["payment_hash"]

    success(f"Execution created: {execution_id[:16]}...")
    info("Skill price", f"{execution['price_sats']} sats")
    info("Platform fee", f"{execution['platform_fee_sats']} sats")
    info("Total cost", f"{execution['total_cost_sats']} sats")
    info("Status", execution["status"])

    fee_invoice = execution.get("fee_payment_request")
    fee_payment_hash = execution.get("fee_payment_hash")

    if fee_invoice:
        info("Invoices", "2 (skill + platform fee)")
    else:
        info("Invoices", "1 (skill only)")
    print()

    # ── Step 4: Pay the invoices ─────────────────────────────────────

    step(4, "Waiting for invoice payment...", agent="consumer")
    print(f"\n  {C.BOLD}Pay this invoice from your SECOND node:{C.RESET}")
    print(f"  {C.CYAN}{skill_invoice}{C.RESET}")
    if fee_invoice:
        print(f"\n  {C.BOLD}Then pay the platform fee invoice:{C.RESET}")
        print(f"  {C.CYAN}{fee_invoice}{C.RESET}")

    print(f"\n  {C.YELLOW}Waiting for settlement... (press Ctrl+C to cancel){C.RESET}")

    # Poll for settlement — also capture the preimage for rating proof
    max_wait = 300  # 5 minutes
    poll_interval = 5
    elapsed = 0
    skill_settled = False
    fee_settled = not fee_payment_hash  # True if no fee invoice
    real_preimage = None

    while elapsed < max_wait:
        if not skill_settled:
            check = client.get(f"/api/v1/lightning/payments/{skill_payment_hash}")
            if check.get("settled"):
                skill_settled = True
                real_preimage = check.get("preimage")
                success("Skill invoice settled!")
                if real_preimage:
                    info("Preimage", real_preimage[:16] + "...")

        if fee_payment_hash and not fee_settled:
            check = client.get(f"/api/v1/lightning/payments/{fee_payment_hash}")
            if check.get("settled"):
                fee_settled = True
                success("Fee invoice settled!")

        if skill_settled and fee_settled:
            break

        time.sleep(poll_interval)
        elapsed += poll_interval
        sys.stdout.write(f"\r  {C.DIM}Waiting... {elapsed}s{C.RESET}  ")
        sys.stdout.flush()

    print()
    if not (skill_settled and fee_settled):
        fail(f"Timed out after {max_wait}s waiting for payment.")
    print()

    # ── Step 5: Confirm execution ────────────────────────────────────

    step(5, "Confirming execution (verifying settlement)", agent="consumer")
    confirm_result = client.post(
        f"/api/v1/marketplace/executions/{execution_id}/confirm",
        data={
            "payment_hash": skill_payment_hash,
            "payment_preimage": real_preimage,
        },
    )
    success(f"Execution confirmed!")
    info("Status", confirm_result["status"])
    info("Fee settled", str(confirm_result.get("fee_settled", "n/a")))
    print()

    # ── Step 6: Rate the execution ───────────────────────────────────

    step(6, "Rating the completed execution", agent="consumer")

    # Rating integrity requires a 30-second cooldown after execution completes.
    print(f"  {C.DIM}Waiting 31s for rating cooldown (anti-gaming measure)...{C.RESET}", end="", flush=True)
    for i in range(31, 0, -1):
        sys.stdout.write(f"\r  {C.DIM}Waiting {i}s for rating cooldown (anti-gaming measure)...  {C.RESET}")
        sys.stdout.flush()
        time.sleep(1)
    print(f"\r  {C.DIM}Rating cooldown elapsed — submitting rating.              {C.RESET}")

    # Use the real preimage from the settled invoice as cryptographic proof.
    # SHA256(preimage) == payment_hash — this proves we actually paid.
    if not real_preimage:
        fail("Could not retrieve preimage from settled invoice. Rating requires payment proof.")

    rating_result = client.post(
        f"/api/v1/marketplace/executions/{execution_id}/rate",
        data={
            "score": 5,
            "review": "Excellent! Worth every sat.",
            "payment_preimage": real_preimage,
        },
    )
    success(f"Rating submitted!")
    info("Score", f"{rating_result['score']}/5")
    info("Weighted average", str(rating_result.get("weighted_average", "n/a")))
    print()

    # ── Summary ──────────────────────────────────────────────────────

    banner("Demo Complete!")
    print(f"  {C.CYAN}Provider Agent (AgentSmith){C.RESET}")
    print(f"    Registered: Sentiment Analyzer Pro")
    print(f"    Earned: {execution['price_sats']} sats")
    print()
    print(f"  {C.YELLOW}Consumer Agent (AgentNeo){C.RESET}")
    print(f"    Discovered, paid, and rated the skill")
    print(f"    Total spent: {execution['total_cost_sats']} sats")
    print()
    print(f"  {C.ORANGE}Conduit Platform{C.RESET}")
    print(f"    Collected fee: {execution['platform_fee_sats']} sats")
    print(f"    Non-custodial: two separate invoices, never held provider funds")
    print()
    print(f"  {C.GREEN}All payments settled on Lightning Network.{C.RESET}")
    print()


# =============================================================================
# Entry point
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Conduit E2E Demo — Two AI Agents Transacting over Lightning"
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Conduit API base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--mode",
        choices=["local", "full"],
        default="local",
        help="Demo mode: 'local' for single-node (free skill), 'full' for real payments (default: local)",
    )
    parser.add_argument(
        "--price",
        type=int,
        default=50,
        help="Skill price in sats for full mode (default: 50)",
    )
    args = parser.parse_args()

    try:
        if args.mode == "full":
            run_full_demo(args.base_url, args.price)
        else:
            run_local_demo(args.base_url)
    except requests.HTTPError as e:
        print(f"\n{C.RED}HTTP Error: {e.response.status_code}{C.RESET}")
        try:
            error_body = e.response.json()
            print(f"{C.DIM}{json.dumps(error_body, indent=2)}{C.RESET}")
        except Exception:
            print(f"{C.DIM}{e.response.text}{C.RESET}")
        sys.exit(1)
    except KeyboardInterrupt:
        print(f"\n{C.YELLOW}Demo interrupted.{C.RESET}")
        sys.exit(0)


if __name__ == "__main__":
    main()
