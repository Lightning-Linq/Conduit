#!/usr/bin/env python
"""Publish the Lightning Linq profile (Nostr kind-0) using Conduit's signing code.

Local one-off. Your nsec is read from the environment and never stored, printed,
or committed. It signs a kind-0 (set_metadata) event with the profile below and
publishes it to the relays, overwriting the previous profile for that key.

Run from the repo root:

    ./.venv/bin/python scripts/publish_ll_profile.py

It prompts you to paste your nsec (input stays hidden), then publishes. The nsec is
never stored, printed, or committed. Set LL_LIGHTNING_ADDRESS (your lud16) in the
environment before running; edit PROFILE below for any wording changes.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys

# Make `conduit` importable whether or not it is pip-installed in the venv.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

from conduit.services.nostr import NostrEvent, NostrKeypair, publish_to_relays  # noqa: E402

# The key this profile must belong to (the one in docs/.well-known/nostr.json).
EXPECTED_PUBKEY = "bd35c6ea941f701f9ec6b0124b227e58da67c457a3180d042a7de6435b087e61"

RELAYS = ["wss://relay.damus.io", "wss://relay.nostr.band", "wss://nos.lol"]

# Your Lightning address (lud16), read from the env so it is not committed.
LIGHTNING_ADDRESS = os.environ.get("LL_LIGHTNING_ADDRESS", "")

PROFILE = {
    "display_name": "Lightning Linq",
    "name": "lightninglinq",
    "about": (
        "Open-source Bitcoin payment rails for AI agents. Discover, pay for, and sell "
        "skills over Lightning. Non-custodial. Nostr-native."
    ),
    "picture": "https://lightninglinq.ai/avatar.png",
    "website": "https://lightninglinq.ai",
    "nip05": "_@lightninglinq.ai",
    "lud16": LIGHTNING_ADDRESS,
}


async def main() -> int:
    if not LIGHTNING_ADDRESS:
        print(
            "Set LL_LIGHTNING_ADDRESS to your Lightning address (lud16) before publishing.",
            file=sys.stderr,
        )
        return 1
    nsec = os.environ.get("NOSTR_NSEC", "").strip()
    if not nsec:
        import getpass
        nsec = getpass.getpass("Paste your nsec (input is hidden), then press Enter: ").strip()
    if not nsec.startswith("nsec1"):
        print("That does not look like an nsec1... key. Aborting.", file=sys.stderr)
        return 1

    keypair = NostrKeypair.from_nsec(nsec)
    if keypair.pubkey_hex != EXPECTED_PUBKEY:
        print("Refusing to publish: this nsec does not match the expected key.", file=sys.stderr)
        print(f"  this nsec -> {keypair.npub}", file=sys.stderr)
        print(f"  expected  -> {EXPECTED_PUBKEY[:16]}... (npub1h56ud655racpl8...)", file=sys.stderr)
        return 1

    event = NostrEvent(
        kind=0,
        content=json.dumps(PROFILE, separators=(",", ":"), ensure_ascii=False),
    )
    event.sign(keypair)

    print(f"Publishing kind-0 profile for {keypair.npub}")
    print(f"  event id: {event.id}")
    results = await publish_to_relays(event, RELAYS, timeout=10.0)
    for relay, ok in results.items():
        print(f"  {'OK  ' if ok else 'FAIL'} {relay}")

    if any(results.values()):
        print("\nPublished. Search your npub in a client (Damus/Primal) in a few seconds.")
        return 0
    print("\nNo relay accepted the event. Check your connection and retry.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
