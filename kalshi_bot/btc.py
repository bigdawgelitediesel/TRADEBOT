"""Quick call on a Kalshi-style BTC question: "in N minutes, above or below $X?"

This is NOT a prediction of direction. Over minutes, the best estimate of where
BTC will be is where it is now; what we can estimate is how *likely* it is to
cross a given strike in the time left, from the distance to the strike and how
much BTC has actually been moving lately (realized volatility from Kraken's
1-minute candles). We assume zero drift and log-normal moves.

    python -m kalshi_bot btc 8 85203
"""
from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass

import requests

KRAKEN = "https://api.kraken.com/0/public"
PAIR = "XBTUSD"
VOL_WINDOW_MIN = 240            # minutes of history for realized vol
MIN_SIGMA_1M = 0.0004           # 0.04%/min floor: BTC is never *that* quiet
MAX_SIGMA_1M = 0.01             # 1%/min cap: don't let one wick dominate


@dataclass
class BtcCall:
    spot: float
    strike: float
    minutes: float
    sigma_1m: float             # realized 1-minute log-return stdev
    p_above: float
    candles_used: int
    spot_age_s: float = 0.0
    source: str = "kraken"

    @property
    def sigma_t_dollars(self) -> float:
        return self.spot * self.sigma_1m * math.sqrt(self.minutes)

    @property
    def distance(self) -> float:
        return self.strike - self.spot

    @property
    def z(self) -> float:
        return math.log(self.strike / self.spot) / (self.sigma_1m * math.sqrt(self.minutes))

    @property
    def verdict(self) -> str:
        return "ABOVE" if self.p_above >= 0.5 else "BELOW"

    @property
    def confidence(self) -> float:
        return max(self.p_above, 1 - self.p_above)

    def fair_cents(self) -> tuple[int, int]:
        """(YES/above, NO/below) fair prices in cents, clamped to 1..99."""
        yes = min(99, max(1, round(self.p_above * 100)))
        return yes, 100 - yes

    def render(self) -> str:
        yes, no = self.fair_cents()
        conf = self.confidence
        label = ("coin flip" if conf < 0.58 else "lean" if conf < 0.7 else
                 "likely" if conf < 0.85 else "high confidence" if conf < 0.97 else "near certain")
        worth = ("Kalshi's price will already be about this. Only worth it if the "
                 f"'{self.verdict.lower()}' side is clearly cheaper than {max(yes, no) - 3}c.")
        if conf < 0.58:
            worth = "Coin flip minus fees. Pass."
        return "\n".join([
            f"BTC spot ${self.spot:,.2f} ({self.source}, {self.spot_age_s:.0f}s old)   "
            f"1m vol {self.sigma_1m * 100:.3f}% from {self.candles_used} candles   "
            f"{self.minutes:g}m σ ≈ ${self.sigma_t_dollars:,.0f}",
            f"Strike ${self.strike:,.0f} is {self.distance:+,.0f} ({self.distance / self.spot * 100:+.2f}%) "
            f"= {abs(self.z):.1f}σ away",
            f"BEST GUESS: {self.verdict}   ({label}, {conf * 100:.0f}%)",
            f"Fair price: above/YES {yes}c   below/NO {no}c",
            worth,
        ])


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def realized_sigma_1m(closes: list[float]) -> float:
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 10:
        return 0.0012   # reasonable BTC default when history is thin
    s = statistics.pstdev(rets)
    return min(MAX_SIGMA_1M, max(MIN_SIGMA_1M, s))


def probability_above(spot: float, strike: float, minutes: float, sigma_1m: float) -> float:
    if spot <= 0 or strike <= 0 or minutes <= 0:
        raise ValueError("spot, strike and minutes must be positive")
    z = math.log(strike / spot) / (sigma_1m * math.sqrt(minutes))
    return 1.0 - _norm_cdf(z)


def make_call(spot: float, strike: float, minutes: float, closes: list[float],
              spot_age_s: float = 0.0) -> BtcCall:
    sigma = realized_sigma_1m(closes)
    return BtcCall(spot=spot, strike=strike, minutes=minutes, sigma_1m=sigma,
                   p_above=probability_above(spot, strike, minutes, sigma),
                   candles_used=max(0, len(closes) - 1), spot_age_s=spot_age_s)


class PriceUnavailable(RuntimeError):
    """No live price source reachable. Never fall back to a stale number silently."""


def _spot_kraken(s, t):
    d = s.get(f"{KRAKEN}/Ticker", params={"pair": PAIR}, timeout=t).json()
    return float(next(iter(d["result"].values()))["c"][0])


def _spot_coinbase(s, t):
    return float(s.get("https://api.coinbase.com/v2/prices/BTC-USD/spot", timeout=t).json()["data"]["amount"])


def _spot_bitstamp(s, t):
    return float(s.get("https://www.bitstamp.net/api/v2/ticker/btcusd/", timeout=t).json()["last"])


def _spot_coingecko(s, t):
    d = s.get("https://api.coingecko.com/api/v3/simple/price",
              params={"ids": "bitcoin", "vs_currencies": "usd"}, timeout=t).json()
    return float(d["bitcoin"]["usd"])


SPOT_SOURCES = [("kraken", _spot_kraken), ("coinbase", _spot_coinbase),
                ("bitstamp", _spot_bitstamp), ("coingecko", _spot_coingecko)]


def live_spot(session: requests.Session | None = None, timeout: float = 6.0) -> tuple[float, str]:
    """First source that answers wins. Raises PriceUnavailable if none do."""
    session = session or requests.Session()
    failures = []
    for name, fn in SPOT_SOURCES:
        try:
            return fn(session, timeout), name
        except Exception as exc:  # try the next one
            failures.append(f"{name}: {type(exc).__name__}")
    raise PriceUnavailable("no live BTC price source reachable (" + "; ".join(failures) + ")")


class KrakenClient:
    def __init__(self, session: requests.Session | None = None, timeout: float = 10.0):
        self.session = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, params: dict) -> dict:
        resp = self.session.get(f"{KRAKEN}{path}", params=params, timeout=self.timeout)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"kraken: {data['error']}")
        return next(iter(data["result"].values()))

    def spot(self) -> float:
        return float(self._get("/Ticker", {"pair": PAIR})["c"][0])

    def closes_1m(self, minutes: int = VOL_WINDOW_MIN) -> list[float]:
        since = int(time.time()) - minutes * 60
        candles = self._get("/OHLC", {"pair": PAIR, "interval": 1, "since": since})
        return [float(c[4]) for c in candles]


def live_call(minutes: float, strike: float, kraken: KrakenClient | None = None) -> BtcCall:
    kraken = kraken or KrakenClient()
    t0 = time.time()
    spot, source = live_spot(kraken.session)
    try:
        closes = kraken.closes_1m()
    except Exception:
        closes = []          # vol falls back to the default; the readout shows "0 candles"
    call = make_call(spot, strike, minutes, closes, spot_age_s=time.time() - t0)
    call.source = source
    return call
