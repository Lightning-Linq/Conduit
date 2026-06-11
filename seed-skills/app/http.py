"""Shared async HTTP for the network skills.

Every network skill calls a FIXED, hardcoded API host and puts user input only into
query parameters — never the host or scheme — so there is no user-controlled-host
SSRF surface. This helper centralises the httpx call (short timeout, redirects off
so an upstream 30x can't bounce us to another host) and is the single point the
tests monkeypatch, keeping the suite offline and deterministic.

Needs the ``net`` extra (httpx); skills importing it are skipped by the loader when
it is not installed.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.registry import SkillError

_TIMEOUT = 10.0
_HEADERS = {"User-Agent": "conduit-seed-skills/0.1"}


async def get_json(url: str, *, params: dict | None = None) -> Any:
    """GET ``url`` (a fixed host) with optional query params and return parsed JSON.

    Raises SkillError (-> HTTP 400) on any transport, status, or JSON error so a
    flaky upstream never surfaces as a 500.
    """
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            resp = await client.get(url, params=params, headers=_HEADERS)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        raise SkillError(f"upstream request failed: {exc}") from exc
    except ValueError as exc:  # invalid JSON body
        raise SkillError(f"upstream returned invalid JSON: {exc}") from exc
