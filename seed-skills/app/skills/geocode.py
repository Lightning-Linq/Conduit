"""geocode — look up coordinates for a place name via Open-Meteo (no key)."""

from __future__ import annotations

from app import http
from app.registry import Skill, SkillError, register

_URL = "https://geocoding-api.open-meteo.com/v1/search"


async def run(input_data: dict) -> dict:
    name = input_data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SkillError("`name` (non-empty string) is required")
    count = input_data.get("count", 5)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 20:
        raise SkillError("`count` must be an integer in 1..20")
    data = await http.get_json(_URL, params={"name": name.strip(), "count": count})
    results = (data or {}).get("results") or []
    return {
        "results": [
            {
                "name": r.get("name"),
                "country": r.get("country"),
                "latitude": r.get("latitude"),
                "longitude": r.get("longitude"),
                "timezone": r.get("timezone"),
            }
            for r in results
        ]
    }


register(
    Skill(
        name="geocode",
        description="Resolve a place name to coordinates via Open-Meteo geocoding.",
        handler=run,
        input_example={"name": "Berlin", "count": 5},
    )
)
