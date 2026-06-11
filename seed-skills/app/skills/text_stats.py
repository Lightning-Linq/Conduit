"""text-stats — word/character/line/sentence counts and reading time."""

from __future__ import annotations

import re

from app.registry import Skill, SkillError, register

_WORD = re.compile(r"\b\w+\b")
_SENTENCE = re.compile(r"[.!?]+(?:\s|$)")


def run(input_data: dict) -> dict:
    text = input_data.get("text")
    if not isinstance(text, str):
        raise SkillError("`text` (string) is required")
    words = _WORD.findall(text)
    return {
        "characters": len(text),
        "characters_no_spaces": len(re.sub(r"\s", "", text)),
        "words": len(words),
        "lines": len(text.splitlines()) if text else 0,
        "sentences": len(_SENTENCE.findall(text)),
        "reading_time_minutes": round(len(words) / 200, 2),
    }


register(
    Skill(
        name="text-stats",
        description="Word, character, line and sentence counts plus estimated reading time.",
        handler=run,
        input_example={"text": "Hello world. This is Conduit."},
    )
)
