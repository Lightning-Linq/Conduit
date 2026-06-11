"""Batch-three network skills — upstream HTTP mocked, so the suite stays offline.

The mock also lets us assert the SSRF-safe invariant: user input lands in query
params, the host stays the hardcoded one.
"""

import pytest
from app import http


@pytest.fixture
def mock_get_json(monkeypatch):
    """Install a fake app.http.get_json returning `payload`; capture the call args."""
    calls = {}

    def _install(payload):
        async def fake(url, *, params=None):
            calls["url"] = url
            calls["params"] = params
            return payload

        monkeypatch.setattr(http, "get_json", fake)
        return calls

    return _install


def test_mempool_fees(client, paid, mock_get_json):
    mock_get_json(
        {"fastestFee": 8, "halfHourFee": 5, "hourFee": 3, "economyFee": 2, "minimumFee": 1}
    )
    r = client.post("/skills/mempool-fees", json=paid("mempool-fees", {}))
    out = r.json()["output"]
    assert out["fastest_fee"] == 8
    assert out["unit"] == "sat/vB"


def test_btc_price_currency(client, paid, mock_get_json):
    mock_get_json({"time": 1700000000, "USD": 65000, "EUR": 60000})
    r = client.post("/skills/btc-price", json=paid("btc-price", {"currency": "eur"}))
    out = r.json()["output"]
    assert out["currency"] == "EUR"
    assert out["price"] == 60000


def test_btc_price_rejects_unknown_currency(client, paid, mock_get_json):
    mock_get_json({})
    r = client.post("/skills/btc-price", json=paid("btc-price", {"currency": "xyz"}))
    assert r.status_code == 400


def test_weather(client, paid, mock_get_json):
    mock_get_json(
        {
            "latitude": 52.5,
            "longitude": 13.4,
            "current_weather": {"temperature": 19.2, "windspeed": 10, "weathercode": 1,
                                "time": "2026-06-11T12:00"},
        }
    )
    r = client.post("/skills/weather", json=paid("weather", {"latitude": 52.5, "longitude": 13.4}))
    assert r.json()["output"]["temperature_c"] == 19.2


def test_weather_rejects_out_of_range(client, paid, mock_get_json):
    mock_get_json({})
    r = client.post("/skills/weather", json=paid("weather", {"latitude": 200, "longitude": 0}))
    assert r.status_code == 400


def test_geocode_keeps_host_fixed(client, paid, mock_get_json):
    calls = mock_get_json(
        {"results": [{"name": "Berlin", "country": "Germany", "latitude": 52.52,
                      "longitude": 13.4, "timezone": "Europe/Berlin"}]}
    )
    # Even a hostile "name" only fills a query param; the host stays hardcoded.
    r = client.post("/skills/geocode", json=paid("geocode", {"name": "Berlin@evil.com"}))
    out = r.json()["output"]
    assert out["results"][0]["country"] == "Germany"
    assert calls["url"].startswith("https://geocoding-api.open-meteo.com/")
    assert calls["params"]["name"] == "Berlin@evil.com"


def test_geocode_requires_name(client, paid, mock_get_json):
    mock_get_json({"results": []})
    r = client.post("/skills/geocode", json=paid("geocode", {"name": "   "}))
    assert r.status_code == 400
