"""opentimestamps — create an OpenTimestamps proof that a hash existed now."""

from __future__ import annotations

import asyncio
import base64
import hashlib

from app import ots
from app.registry import Skill, SkillError, register


def _digest(input_data: dict) -> bytes:
    sha256_hex = input_data.get("sha256")
    if isinstance(sha256_hex, str) and sha256_hex.strip():
        try:
            digest = bytes.fromhex(sha256_hex.strip())
        except ValueError as exc:
            raise SkillError(f"`sha256` must be hex: {exc}") from exc
        if len(digest) != 32:
            raise SkillError("`sha256` must be a 32-byte (64 hex char) digest")
        return digest
    data = input_data.get("data")
    if isinstance(data, str):
        return hashlib.sha256(data.encode("utf-8")).digest()
    raise SkillError("provide `sha256` (64 hex chars) or `data` (a string to hash)")


async def run(input_data: dict) -> dict:
    digest = _digest(input_data)
    # The calendar submission blocks on HTTP — run it off the event loop.
    loop = asyncio.get_running_loop()
    ots_bytes = await loop.run_in_executor(None, ots.stamp_digest, digest)
    return {
        "sha256": digest.hex(),
        "ots_proof_base64": base64.b64encode(ots_bytes).decode("ascii"),
        "status": "pending",
        "note": (
            "Pending Bitcoin confirmation. Upgrade and verify later with the `ots` "
            "client or any OpenTimestamps verifier."
        ),
    }


register(
    Skill(
        name="opentimestamps",
        description="Create an OpenTimestamps proof committing a SHA-256 hash to Bitcoin "
        "(pending until confirmed).",
        handler=run,
        input_example={"data": "hello"},
    )
)
