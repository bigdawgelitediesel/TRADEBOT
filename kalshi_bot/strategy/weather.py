"""Weather strategy: price Kalshi daily-high-temperature buckets off NWS forecasts.

Model: the settled high is Normal(forecast, sigma), with sigma growing with lead
time. Each market is a bucket (between / greater / less) over whole-degree
settlements, so we integrate the normal over the bucket with 0.5°F continuity
correction. We buy whichever side (YES or NO) has positive expected value after
the taker fee, if that EV clears `min_edge_cents`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timezone

from ..config import City, StrategyConfig
from ..fees import fee_per_contract_cents


@dataclass
class Candidate:
    ticker: str
    event_ticker: str
    city: str
    market_date: date
    side: str               # "yes" or "no"
    price_cents: int        # ask we'd pay for that side
    model_prob: float       # P(that side wins)
    edge_cents: float       # EV per contract after fee
    forecast_f: float
    sigma: float
    title: str

    def __str__(self) -> str:
        return (f"{self.ticker:<28} buy {self.side.upper():<3} @ {self.price_cents:>2}c  "
                f"p={self.model_prob:.2f}  edge={self.edge_cents:+.1f}c  "
                f"fcst={self.forecast_f:.0f}F σ={self.sigma}  {self.title}")


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _edge(value: float, lo: bool) -> float:
    """Shift whole-degree strikes by half a degree so integer settlements fall cleanly."""
    if float(value).is_integer():
        return value - 0.5 if lo else value + 0.5
    return value


def bucket_probability(market: dict, mean: float, sigma: float) -> float | None:
    """P(settled high lands in this market's YES bucket)."""
    st = (market.get("strike_type") or "").lower()
    floor = market.get("floor_strike")
    cap = market.get("cap_strike")
    if st == "between" and floor is not None and cap is not None:
        lo, hi = _edge(float(floor), lo=True), _edge(float(cap), lo=False)
    elif st in ("greater", "greater_or_equal") and floor is not None:
        f = float(floor)
        lo = f + 0.5 if (st == "greater" and f.is_integer()) else _edge(f, lo=True)
        hi = math.inf
    elif st in ("less", "less_or_equal") and cap is not None:
        c = float(cap)
        hi = c - 0.5 if (st == "less" and c.is_integer()) else _edge(c, lo=False)
        lo = -math.inf
    else:
        return None
    p = _norm_cdf((hi - mean) / sigma) - _norm_cdf((lo - mean) / sigma)
    return min(max(p, 0.0), 1.0)


def parse_market_date(event_ticker: str) -> date | None:
    """KXHIGHNY-25OCT06 -> 2025-10-06."""
    try:
        token = event_ticker.split("-")[1]
        return datetime.strptime(token, "%y%b%d").date()
    except (IndexError, ValueError):
        return None


def _hours_to_close(market: dict, now: datetime) -> float:
    raw = market.get("close_time") or market.get("expiration_time")
    if not raw:
        return math.inf
    close = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    return (close - now).total_seconds() / 3600.0


class WeatherStrategy:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def sigma_for(self, lead_days: int) -> float:
        table = self.cfg.sigma_by_lead_day
        return table[min(max(lead_days, 0), len(table) - 1)]

    def evaluate(self, city: City, markets: list[dict], highs_f: dict[date, float],
                 now: datetime | None = None) -> list[Candidate]:
        now = now or datetime.now(timezone.utc)
        today = datetime.now(timezone.utc).astimezone().date()
        out: list[Candidate] = []
        for m in markets:
            if m.get("status") not in (None, "open", "active"):
                continue
            mdate = parse_market_date(m.get("event_ticker", ""))
            if mdate is None or mdate not in highs_f:
                continue
            lead = (mdate - today).days
            if lead < 0 or lead > self.cfg.max_lead_days:
                continue
            if _hours_to_close(m, now) < self.cfg.min_hours_to_close:
                continue
            forecast = highs_f[mdate]
            sigma = self.sigma_for(lead)
            p_yes = bucket_probability(m, forecast, sigma)
            if p_yes is None:
                continue
            for side, p in (("yes", p_yes), ("no", 1.0 - p_yes)):
                ask = m.get(f"{side}_ask")
                if not ask or not 1 <= int(ask) <= 99:
                    continue
                if not self.cfg.min_prob <= p <= self.cfg.max_prob:
                    continue
                edge = p * 100.0 - int(ask) - fee_per_contract_cents(int(ask))
                if edge >= self.cfg.min_edge_cents:
                    out.append(Candidate(
                        ticker=m["ticker"], event_ticker=m.get("event_ticker", ""),
                        city=city.name, market_date=mdate, side=side, price_cents=int(ask),
                        model_prob=p, edge_cents=edge, forecast_f=forecast, sigma=sigma,
                        title=m.get("subtitle") or m.get("title", ""),
                    ))
        out.sort(key=lambda c: c.edge_cents, reverse=True)
        return out
