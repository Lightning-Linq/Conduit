"""Server-side payment verification for keyless skills.

The preimage is the bearer proof a Lightning invoice settled: SHA256(preimage)
== payment_hash. Conduit only calls the webhook after settlement, so requiring a
valid preimage gates execution against direct, unpaid calls to this endpoint.

NOTE: this proves the preimage matches the hash, not that *this* provider issued
the invoice. A production provider should ALSO confirm payment_hash maps to an
invoice it issued and was actually paid (look it up against its own node). For the
keyless seed skills — run by the same operator as the Conduit node — the hash
match is the intended gate; the invoice lookup is the documented extension point.
"""

from __future__ import annotations

import hashlib
import hmac


def verify_payment_proof(payment_hash: str, payment_preimage: str) -> bool:
    """True iff SHA256(preimage) == payment_hash (both 32-byte hex strings)."""
    try:
        preimage = bytes.fromhex(payment_preimage)
        expected = bytes.fromhex(payment_hash)
    except ValueError:
        return False
    if len(preimage) != 32 or len(expected) != 32:
        return False
    return hmac.compare_digest(hashlib.sha256(preimage).digest(), expected)
