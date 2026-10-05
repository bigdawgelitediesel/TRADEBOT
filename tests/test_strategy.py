from datetime import date, datetime, timedelta, timezone

import pytest

from kalshi_bot.config import City, StrategyConfig
from kalshi_bot.nws import parse_max_temps
from kalshi_bot.strategy import WeatherStrategy, bucket_probability, parse_market_date

CITY = City(name="New York", series="KXHIGHNY", lat=40.78, lon=-73.97, tz="America/New_York")


def test_parse_market_date():
    assert parse_market_date("KXHIGHNY-25OCT06") == date(2025, 10, 6)
    assert parse_market_date("KXHIGHNY-26JAN01-B62.5") == date(2026, 1, 1)
    assert parse_market_date("garbage") is None


def test_between_bucket_centered_on_forecast():
    m = {"strike_type": "between", "floor_strike": 70, "cap_strike": 71}
    p = bucket_probability(m, mean=70.5, sigma=2.0)
    assert 0.35 < p < 0.40          # P(69.5 < X < 71.5) with sigma 2 ≈ 0.383


def test_tails_sum_with_middle():
    low = bucket_probability({"strike_type": "less", "cap_strike": 70}, 70.5, 2.0)      # X <= 69
    mid = bucket_probability({"strike_type": "between", "floor_strike": 70, "cap_strike": 71}, 70.5, 2.0)
    high = bucket_probability({"strike_type": "greater", "floor_strike": 71}, 70.5, 2.0)  # X >= 72
    assert low + mid + high == pytest.approx(1.0, abs=1e-9)


def test_half_degree_strikes_used_verbatim():
    a = bucket_probability({"strike_type": "greater", "floor_strike": 71.5}, 70.5, 2.0)
    b = bucket_probability({"strike_type": "greater", "floor_strike": 71}, 70.5, 2.0)
    assert a == pytest.approx(b)


def test_unknown_strike_type_is_none():
    assert bucket_probability({"strike_type": "weird"}, 70, 2) is None


def _market(ticker, event, st, floor=None, cap=None, yes_ask=50, no_ask=50, hours=12):
    close = datetime.now(timezone.utc) + timedelta(hours=hours)
    return {"ticker": ticker, "event_ticker": event, "status": "open", "strike_type": st,
            "floor_strike": floor, "cap_strike": cap, "yes_ask": yes_ask, "no_ask": no_ask,
            "yes_bid": yes_ask - 2, "no_bid": no_ask - 2, "close_time": close.isoformat(),
            "subtitle": f"{floor}-{cap}"}


def _today_event():
    return "KXHIGHNY-" + date.today().strftime("%y%b%d").upper()


def test_evaluate_finds_mispriced_bucket():
    ev = _today_event()
    # Forecast 75F. 74-75 at 20c YES is cheap; 72-73 at 30c YES is rich (buy NO at 72c).
    markets = [
        _market("A", ev, "between", 74, 75, yes_ask=20, no_ask=82),
        _market("B", ev, "between", 72, 73, yes_ask=30, no_ask=72),
        _market("C", ev, "between", 80, 81, yes_ask=3, no_ask=98),   # out of prob band
    ]
    strat = WeatherStrategy(StrategyConfig(min_edge_cents=5))
    cands = strat.evaluate(CITY, markets, {date.today(): 75.0})
    picks = {(c.ticker, c.side) for c in cands}
    assert ("A", "yes") in picks
    assert ("B", "no") in picks
    assert all(c.ticker != "C" for c in cands)
    assert cands[0].edge_cents >= cands[-1].edge_cents


def test_evaluate_skips_closing_soon_and_far_dates():
    ev = _today_event()
    strat = WeatherStrategy(StrategyConfig(min_edge_cents=1, min_hours_to_close=3, max_lead_days=1))
    soon = _market("S", ev, "between", 74, 75, yes_ask=20, hours=1)
    far_ev = "KXHIGHNY-" + (date.today() + timedelta(days=5)).strftime("%y%b%d").upper()
    far = _market("F", far_ev, "between", 74, 75, yes_ask=20)
    highs = {date.today(): 75.0, date.today() + timedelta(days=5): 75.0}
    assert strat.evaluate(CITY, [soon, far], highs) == []


def test_parse_max_temps_converts_and_dates():
    data = {"properties": {"maxTemperature": {"uom": "wmoUnit:degC", "values": [
        {"validTime": "2026-10-05T06:00:00+00:00/PT24H", "value": 20.0},
        {"validTime": "2026-10-06T06:00:00+00:00/PT24H", "value": None},
        {"validTime": "2026-10-07T06:00:00+00:00/PT24H", "value": 25.0},
    ]}}}
    out = parse_max_temps(data, "America/New_York")
    assert out[date(2026, 10, 5)] == pytest.approx(68.0)
    assert date(2026, 10, 6) not in out
    assert out[date(2026, 10, 7)] == pytest.approx(77.0)
