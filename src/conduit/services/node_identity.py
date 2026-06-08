"""The node's Nostr identity keypair — a single source for both front doors.

This one keypair is the node's *provider* identity: it signs published skill
events (kind 38383) AND the payer bindings on rating attestations. The MCP server
and the REST routers must use the SAME key, or a skill published under one key
would carry bindings signed by another and fail verification. Loaded from
settings.nostr_private_key (nsec or hex), else generated once and cached.
"""

from __future__ import annotations

import sys

from conduit.core.config import settings
from conduit.services.nostr import NostrKeypair

_node_keys: NostrKeypair | None = None


def get_node_keypair() -> NostrKeypair:
    """Get or create the node's Nostr keypair (cached for the process)."""
    global _node_keys
    if _node_keys is None:
        key = settings.nostr_private_key
        if key:
            _node_keys = (
                NostrKeypair.from_nsec(key)
                if key.startswith("nsec")
                else NostrKeypair.from_hex(key)
            )
            print(f"[nostr] Loaded key: {_node_keys.npub[:20]}...", file=sys.stderr)
        else:
            _node_keys = NostrKeypair.generate()
            print(
                f"[nostr] Generated new keypair: {_node_keys.npub}\n"
                f"[nostr] Save this nsec to persist identity: {_node_keys.nsec}",
                file=sys.stderr,
            )
    return _node_keys
