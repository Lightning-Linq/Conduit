"""weather — current conditions for a latitude/longitude via Open-Meteo (no key)."""

from __future__ import annotations

from app import http
from app.registry import Skill, SkillError, register

_URL = "https://api.open-meteo.com/v1/forecast"


def _coord(value: object, name: str, lo: float, hi: float) -> float:
    # bool is an int subclass — reject it so `true` can't pass as a coordinate.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SkillError(f"`{name}` (number) is required")
    if not lo <= value <= hi:
        raise SkillError(f"`{name}` must be between {lo} and {hi}")
    return float(value)


async def run(input_data: dict) -> dict:
    latitude = _coord(input_data.get("latitude"), "latitude", -90, 90)
    longitude = _coord(input_data.get("longitude"), "longitude", -180, 180)
    data = await http.get_json(
        _URL,
        params={"latitude": latitude, "longitude": longitude, "current_weather": "true"},
    )
    current = (data or {}).get("current_weather")
    if not isinstance(current, dict):
        raise SkillError("unexpected upstream response")
    return {
        "latitude": data.get("latitude"),
        "longitude": data.get("longitude"),
        "temperature_c": current.get("temperature"),
        "windspeed": current.get("windspeed"),
        "weathercode": current.get("weathercode"),
        "time": current.get("time"),
    }


register(
    Skill(
        name="weather",
        description="Current weather (temperature, windspeed) for a lat/lon via Open-Meteo.",
        handler=run,
        input_example={"latitude": 52.52, "longitude": 13.40},
    )
)
