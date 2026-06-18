"""The node's Nostr identity keypair, a single source for both front doors.

This one keypair is the node's *provider* identity: it signs published skill
events (kind 38383) AND the payer bindings on rating attestations. The MCP server
and the REST routers must use the SAME key, or a skill published under one key
would carry bindings signed by another and fail verification.

Resolution order: NOSTR_PRIVATE_KEY (nsec or hex), then the persisted
credentials/nostr.nsec file, then a freshly generated key that is written to that
file. The file fallback is what keeps separate MCP and REST processes on the same
identity when the env var is unset (N11).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from conduit.core.config import settings
from conduit.services.nostr import NostrKeypair

_node_keys: NostrKeypair | None = None
# How the cached key was obtained: "env" | "file" | "generated" | None (S11).
_key_source: str | None = None

# <project root>/credentials/nostr.nsec (matches the MCP persistence path).
_NSEC_FILE = Path(__file__).resolve().parents[3] / "credentials" / "nostr.nsec"


def _persist_nsec(keypair: NostrKeypair) -> None:
    """Write the generated nsec to a 0600 file so other processes reuse it."""
    try:
        _NSEC_FILE.parent.mkdir(exist_ok=True)
        _NSEC_FILE.write_text(keypair.nsec + "\n")
        os.chmod(_NSEC_FILE, 0o600)
    except OSError as e:
        print(f"[nostr] Could not persist key to {_NSEC_FILE}: {e}", file=sys.stderr)


def get_node_keypair() -> NostrKeypair:
    """Get or create the node's Nostr keypair (cached for the process).

    env NOSTR_PRIVATE_KEY > persisted credentials/nostr.nsec > generate + persist.
    The file fallback keeps the MCP server and REST API (separate processes) on the
    same provider identity (N11).
    """
    global _node_keys, _key_source
    if _node_keys is not None:
        return _node_keys

    key = settings.nostr_private_key
    if key:
        _node_keys = (
            NostrKeypair.from_nsec(key)
            if key.startswith("nsec")
            else NostrKeypair.from_hex(key)
        )
        _key_source = "env"
        print(f"[nostr] Loaded key from env: {_node_keys.npub[:20]}...", file=sys.stderr)
        return _node_keys

    if _NSEC_FILE.exists():
        try:
            _node_keys = NostrKeypair.from_nsec(_NSEC_FILE.read_text().strip())
            _key_source = "file"
            print(
                f"[nostr] Loaded key from {_NSEC_FILE}: {_node_keys.npub[:20]}...",
                file=sys.stderr,
            )
            return _node_keys
        except Exception as e:  # noqa: BLE001 - corrupt file: fall through to regenerate
            print(f"[nostr] Ignoring unreadable {_NSEC_FILE}: {e}", file=sys.stderr)

    _node_keys = NostrKeypair.generate()
    _key_source = "generated"
    _persist_nsec(_node_keys)
    # H9: never print the nsec itself; it now signs provider bindings too.
    print(
        f"[nostr] Generated new keypair {_node_keys.npub} and saved it to "
        f"{_NSEC_FILE} (mode 0600). Set NOSTR_PRIVATE_KEY to pin this identity.",
        file=sys.stderr,
    )
    return _node_keys


def get_key_source() -> str | None:
    """How the current node key was obtained: 'env' | 'file' | 'generated' | None."""
    return _key_source
