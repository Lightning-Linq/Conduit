"""hash-digest — cryptographic hash of UTF-8 text."""

from __future__ import annotations

import hashlib

from app.registry import Skill, SkillError, register

_ALGORITHMS = {"sha256", "sha512", "sha1", "md5", "sha3_256", "blake2b"}


def run(input_data: dict) -> dict:
    text = input_data.get("text")
    if not isinstance(text, str):
        raise SkillError("`text` (string) is required")
    algorithm = str(input_data.get("algorithm") or "sha256").lower()
    if algorithm not in _ALGORITHMS:
        raise SkillError(f"unsupported algorithm {algorithm!r}; choose from {sorted(_ALGORITHMS)}")
    digest = hashlib.new(algorithm, text.encode("utf-8")).hexdigest()
    return {"algorithm": algorithm, "hex": digest}


register(
    Skill(
        name="hash-digest",
        description="Cryptographic hash of UTF-8 text (sha256/sha512/sha1/md5/sha3_256/blake2b).",
        handler=run,
        input_example={"text": "hello world", "algorithm": "sha256"},
    )
)
