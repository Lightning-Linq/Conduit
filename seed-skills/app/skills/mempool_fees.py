"""mempool-fees — recommended Bitcoin fee rates from mempool.space."""

from __future__ import annotations

from app import http
from app.registry import Skill, SkillError, register

_URL = "https://mempool.space/api/v1/fees/recommended"


async def run(input_data: dict) -> dict:
    fees = await http.get_json(_URL)
    if not isinstance(fees, dict):
        raise SkillError("unexpected upstream response")
    return {
        "fastest_fee": fees.get("fastestFee"),
        "half_hour_fee": fees.get("halfHourFee"),
        "hour_fee": fees.get("hourFee"),
        "economy_fee": fees.get("economyFee"),
        "minimum_fee": fees.get("minimumFee"),
        "unit": "sat/vB",
    }


register(
    Skill(
        name="mempool-fees",
        description="Recommended Bitcoin fee rates (sat/vB) from mempool.space.",
        handler=run,
        input_example={},
    )
)
