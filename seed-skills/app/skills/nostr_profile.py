"""nostr-profile — look up a Nostr user's kind-0 profile by pubkey."""

from __future__ import annotations

import json

from app import nostr
from app.registry import Skill, SkillError, register


async def run(input_data: dict) -> dict:
    raw = input_data.get("pubkey")
    if not isinstance(raw, str) or not raw.strip():
        raise SkillError("`pubkey` (hex or npub) is required")
    try:
        pubkey = nostr.normalize_pubkey(raw.strip())
    except ValueError as exc:
        raise SkillError(f"invalid pubkey: {exc}") from exc

    event, relay = await nostr.fetch_profile_event(pubkey)
    if event is None:
        return {"pubkey": pubkey, "found": False, "relay": None, "profile": None}
    try:
        profile = json.loads(event.get("content") or "{}")
    except (ValueError, TypeError):
        profile = None  # malformed metadata content
    return {
        "pubkey": pubkey,
        "found": True,
        "relay": relay,
        "created_at": event.get("created_at"),
        "profile": profile,
    }


register(
    Skill(
        name="nostr-profile",
        description="Look up a Nostr user's kind-0 profile (name, about, nip05, lud16) by pubkey.",
        handler=run,
        input_example={"pubkey": "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"},
    )
)
