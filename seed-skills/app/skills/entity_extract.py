"""entity-extract — pull structured entities (emails, URLs, IPs, BTC/LN) from text."""

from __future__ import annotations

import re

from app.registry import Skill, SkillError, register

_PATTERNS = {
    "emails": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "urls": re.compile(r"https?://[^\s<>\"]+"),
    "ipv4": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
    "bitcoin_addresses": re.compile(r"\b(?:bc1[a-z0-9]{20,90}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"),
    "lightning_invoices": re.compile(r"\bln(?:bc|tb)[0-9][a-z0-9]+\b", re.IGNORECASE),
    "hashtags": re.compile(r"(?<!\w)#\w+"),
}


def run(input_data: dict) -> dict:
    text = input_data.get("text")
    if not isinstance(text, str):
        raise SkillError("`text` (string) is required")
    # dict.fromkeys dedupes while preserving first-seen order.
    return {
        name: list(dict.fromkeys(pattern.findall(text)))
        for name, pattern in _PATTERNS.items()
    }


register(
    Skill(
        name="entity-extract",
        description="Best-effort extraction of emails, URLs, IPv4s, Bitcoin/Lightning, hashtags.",
        handler=run,
        input_example={"text": "ping a@b.com or bc1qexample, see https://lightninglinq.com"},
    )
)
