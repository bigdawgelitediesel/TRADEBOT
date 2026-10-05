"""National Weather Service forecast client (api.weather.gov, free, no key).

We use the gridpoint "raw" forecast, which exposes maxTemperature per local day
in degrees C. The /points lookup (lat/lon -> grid) is cached in memory.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

USER_AGENT = "kalshi-weather-bot (github.com/bigdawgelitediesel/TRADEBOT)"
BASE = "https://api.weather.gov"


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


class NWSClient:
    def __init__(self, session: requests.Session | None = None, timeout: float = 20.0):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/geo+json"})
        self.timeout = timeout
        self._grid_cache: dict[tuple[float, float], str] = {}

    def _get(self, url: str) -> dict:
        resp = self.session.get(url, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def grid_url(self, lat: float, lon: float) -> str:
        key = (round(lat, 4), round(lon, 4))
        if key not in self._grid_cache:
            props = self._get(f"{BASE}/points/{key[0]},{key[1]}")["properties"]
            self._grid_cache[key] = props["forecastGridData"]
        return self._grid_cache[key]

    def daily_highs_f(self, lat: float, lon: float, tz: str) -> dict[date, float]:
        """Forecast daily high (°F) keyed by local calendar date."""
        data = self._get(self.grid_url(lat, lon))
        return parse_max_temps(data, tz)


def parse_max_temps(gridpoint_json: dict, tz: str) -> dict[date, float]:
    zone = ZoneInfo(tz)
    out: dict[date, float] = {}
    block = gridpoint_json["properties"]["maxTemperature"]
    is_f = "degF" in block.get("uom", "wmoUnit:degC")
    for entry in block.get("values", []):
        if entry.get("value") is None:
            continue
        start = datetime.fromisoformat(entry["validTime"].split("/")[0])
        # NWS max-temp periods start in the early local morning; shifting by a few
        # hours puts the period squarely on the local day it describes.
        local_day = (start.astimezone(zone) + timedelta(hours=6)).date()
        temp = float(entry["value"])
        out[local_day] = temp if is_f else c_to_f(temp)
    return out
