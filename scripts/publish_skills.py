#!/usr/bin/env python
"""Publish Lightning Linq's seed skills to Nostr (kind-38383) using Conduit's code.

Reads the catalog from docs/skills.json, builds a signed kind-38383 listing for each
skill (Conduit's skill_to_event), and publishes to the relays. marketplace.html
subscribes to kind-38383, so the listings show live once this runs.

Each listing's endpoint_url points at ENDPOINT_BASE, the host you will deploy the
seed-skills webhook to. Until that host is live the listings are discoverable but not
executable. The d-tag is the skill name, so re-running replaces rather than duplicates.

Run from the repo root:

    export LL_LIGHTNING_ADDRESS=you@example.com   # your lud16 payout address
    ./.venv/bin/python scripts/publish_skills.py
    # paste your nsec at the hidden prompt

Dry run (build and print the events, no key, no publish):

    DRY_RUN=1 ./.venv/bin/python scripts/publish_skills.py
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from conduit.services.nostr import NostrKeypair, publish_to_relays, skill_to_event  # noqa: E402

# Guardrail (not a secret): refuse to publish if the pasted nsec is not this key.
EXPECTED_PUBKEY = "bd35c6ea941f701f9ec6b0124b227e58da67c457a3180d042a7de6435b087e61"
RELAYS = ["wss://relay.damus.io", "wss://relay.nostr.band", "wss://nos.lol"]

# Where the seed-skills webhook will be deployed; each endpoint is BASE/<skill-name>.
# Change this to your real host before the listings can actually execute.
ENDPOINT_BASE = "https://skills.lightninglinq.ai/skills"
# Lightning address payments route to (your lud16). Read from env so it is not committed.
LIGHTNING_ADDRESS = os.environ.get("LL_LIGHTNING_ADDRESS", "")

CATALOG = pathlib.Path(__file__).resolve().parent.parent / "docs" / "skills.json"


def _skill_dicts(lightning_address: str) -> list[dict]:
    skills = json.loads(CATALOG.read_text())["skills"]
    return [
        {
            "id": s["name"],  # stable NIP-33 d-tag
            "name": s["name"],
            "description": s.get("description", ""),
            "category": s.get("category", "general"),
            "price_sats": s.get("price_sats", 0),
            "provider_name": s.get("provider_name", "Lightning Linq"),
            "provider_lightning_address": lightning_address,
            "endpoint_url": f"{ENDPOINT_BASE}/{s['name']}",
        }
        for s in skills
    ]


async def main() -> int:
    dry = os.getenv("DRY_RUN") == "1"
    address = LIGHTNING_ADDRESS or ("you@example.com" if dry else "")
    if not address:
        print(
            "Set LL_LIGHTNING_ADDRESS to your Lightning address (lud16) before publishing.",
            file=sys.stderr,
        )
        return 1
    skills = _skill_dicts(address)

    if dry:
        keypair = NostrKeypair.generate()
        print(f"DRY RUN: building {len(skills)} kind-38383 events (throwaway key, no publish)\n")
    else:
        import getpass

        nsec = os.environ.get("NOSTR_NSEC", "").strip() or getpass.getpass(
            "Paste your nsec (input is hidden), then press Enter: "
        ).strip()
        if not nsec.startswith("nsec1"):
            print("That does not look like an nsec1... key. Aborting.", file=sys.stderr)
            return 1
        keypair = NostrKeypair.from_nsec(nsec)
        if keypair.pubkey_hex != EXPECTED_PUBKEY:
            print("Refusing to publish: this nsec is not the Lightning Linq key.", file=sys.stderr)
            print(f"  this nsec -> {keypair.npub}", file=sys.stderr)
            return 1
        print(f"Publishing {len(skills)} skills as {keypair.npub}\n")

    published = 0
    for s in skills:
        event = skill_to_event(s, keypair)
        if dry:
            print(
                f"  {s['name']:<16}{s['price_sats']:>4} sats  "
                f"{s['category']:<10} -> {s['endpoint_url']}"
            )
            continue
        results = await publish_to_relays(event, RELAYS, timeout=10.0)
        good = sum(1 for ok in results.values() if ok)
        published += 1 if good else 0
        print(f"  {s['name']:<16}{good}/{len(RELAYS)} relays")

    if dry:
        print("\nDry run complete. Run without DRY_RUN to publish.")
        return 0
    print(f"\nPublished {published}/{len(skills)} skills to at least one relay.")
    print("Check lightninglinq.ai/marketplace; it should now show live listings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
