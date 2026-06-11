"""Skill registry: name -> handler + metadata, for dispatch and the catalog.

A skill module builds a :class:`Skill` and calls :func:`register`; importing the
module (via ``app.skills``) is what populates ``REGISTRY``. Handlers may be sync
or async and return the skill's output value (the app wraps it as ``{"output": ...}``).
Raise :class:`SkillError` for bad input — the app maps it to HTTP 400.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

SkillHandler = Callable[[dict[str, Any]], Any]


class SkillError(Exception):
    """A skill rejected its input (maps to HTTP 400)."""


@dataclass(frozen=True)
class Skill:
    """A registered skill: its name, a one-line description, handler, and example input."""

    name: str
    description: str
    handler: SkillHandler
    input_example: dict[str, Any] = field(default_factory=dict)


REGISTRY: dict[str, Skill] = {}


def register(skill: Skill) -> Skill:
    """Add a skill to the registry; raises ValueError on a duplicate name."""
    if skill.name in REGISTRY:
        raise ValueError(f"duplicate skill registered: {skill.name}")
    REGISTRY[skill.name] = skill
    return skill
