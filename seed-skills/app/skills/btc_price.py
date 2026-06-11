"""btc-price — current BTC price in a fiat currency from mempool.space."""

from __future__ import annotations

from app import http
from app.registry import Skill, SkillError, register

_URL = "https://mempool.space/api/v1/prices"
_CURRENCIES = {"USD", "EUR", "GBP", "CAD", "CHF", "AUD", "JPY"}


async def run(input_data: dict) -> dict:
    currency = str(input_data.get("currency") or "USD").upper()
    if currency not in _CURRENCIES:
        raise SkillError(f"unsupported currency {currency!r}; choose from {sorted(_CURRENCIES)}")
    prices = await http.get_json(_URL)
    if not isinstance(prices, dict) or currency not in prices:
        raise SkillError("unexpected upstream response")
    return {"currency": currency, "price": prices[currency], "as_of": prices.get("time")}


register(
    Skill(
        name="btc-price",
        description="Current BTC price in a fiat currency (USD/EUR/GBP/CAD/CHF/AUD/JPY).",
        handler=run,
        input_example={"currency": "USD"},
    )
)
