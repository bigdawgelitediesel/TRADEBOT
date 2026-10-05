from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kalshi_bot.config import City, Config, RiskConfig, StrategyConfig
from kalshi_bot.executor import Executor
from kalshi_bot.risk import RiskManager
from kalshi_bot.runner import Runner
from kalshi_bot.state import Position, State
from kalshi_bot.strategy import Candidate


def cand(ticker="T1", event="E1", price=40, edge=8.0):
    return Candidate(ticker=ticker, event_ticker=event, city="X", market_date=date.today(), side="yes",
                     price_cents=price, model_prob=0.6, edge_cents=edge, forecast_f=70, sigma=2, title="")


def test_sizing_respects_per_trade_cost():
    rm = RiskManager(RiskConfig(max_contracts_per_trade=50, max_cost_per_trade_cents=500,
                                kill_switch_file="nope-not-here"))
    s = rm.check(cand(price=40), State(), 0)
    assert s.ok and s.count == 11 and s.cost_cents == 459   # 12*40 + 21c fee = 501 > 500, so 11


def test_sizing_blocks_duplicates_and_daily_loss():
    rm = RiskManager(RiskConfig(kill_switch_file="nope-not-here", max_daily_loss_cents=100))
    st = State()
    st.positions["T1"] = Position("T1", "E1", "yes", 1, 40, 41, "now", True)
    assert not rm.check(cand("T1"), st, 0).ok
    assert "event" in rm.check(cand("T2", "E1"), st, 0).reason
    st.daily_realized_cents = -150
    assert "daily loss" in rm.check(cand("T3", "E9"), st, 0).reason


def test_kill_switch(tmp_path, monkeypatch):
    stop = tmp_path / "STOP"
    stop.write_text("")
    rm = RiskManager(RiskConfig(kill_switch_file=str(stop)))
    assert "kill switch" in rm.check(cand(), State(), 0).reason


def test_paper_executor_records_position():
    st = State()
    ex = Executor(client=None, live=False)
    sizing = RiskManager(RiskConfig(kill_switch_file="nope")).check(cand(), st, 0)
    pos = ex.execute(cand(), sizing, st)
    assert pos.paper and st.positions["T1"].count == sizing.count and st.daily_trades == 1


def test_live_executor_requires_client():
    with pytest.raises(ValueError):
        Executor(client=None, live=True)


def test_state_roundtrip_and_settle(tmp_path):
    st = State()
    st.roll_day(date(2026, 10, 5))
    st.record_fill(Position("T1", "E1", "yes", 2, 40, 82, "now", True, last_mark_cents=45))
    assert st.unrealized_cents() == 90 - 82
    p = tmp_path / "s.json"
    st.save(p)
    st2 = State.load(p)
    assert st2.positions["T1"].count == 2 and st2.day == "2026-10-05"
    st2.settle("T1", 200)
    assert st2.daily_realized_cents == 118 and "T1" not in st2.positions


# ------------------------------------------------------------------ runner e2e (mocked)
class FakeKalshi:
    def __init__(self, markets):
        self.markets = markets
        self.orders = []

    def get_markets(self, series_ticker=None, **kw):
        return [m for m in self.markets if m["ticker"].startswith(series_ticker)]

    def get_market(self, ticker):
        return next(m for m in self.markets if m["ticker"] == ticker)

    def exchange_status(self):
        return {"trading_active": True, "exchange_active": True}

    def get_orders(self, status="resting"):
        return []

    def place_order(self, ticker, side, count, price_cents, action="buy", client_order_id=None):
        self.orders.append((ticker, side, count, price_cents))
        return {"order_id": "o1", "status": "executed", "fill_count": count}


class FakeNWS:
    def daily_highs_f(self, lat, lon, tz):
        return {date.today(): 75.0}


def _mk(ticker, floor, cap, yes_ask, no_ask):
    ev = "KXHIGHNY-" + date.today().strftime("%y%b%d").upper()
    return {"ticker": ticker, "event_ticker": ev, "status": "open", "strike_type": "between",
            "floor_strike": floor, "cap_strike": cap, "yes_ask": yes_ask, "no_ask": no_ask,
            "yes_bid": yes_ask - 1, "no_bid": no_ask - 1,
            "close_time": (datetime.now(timezone.utc) + timedelta(hours=10)).isoformat()}


def _cfg(tmp_path, mode):
    return Config(env="demo", mode=mode,
                  cities=[City("New York", "KXHIGHNY", 40.78, -73.97, "America/New_York")],
                  strategy=StrategyConfig(min_edge_cents=5),
                  risk=RiskConfig(max_positions_per_event=2, kill_switch_file="nope"),
                  state_path=tmp_path / "state.json")


def test_runner_paper_cycle(tmp_path):
    kal = FakeKalshi([_mk("KXHIGHNY-X-B74.5", 74, 75, 20, 82), _mk("KXHIGHNY-X-B72.5", 72, 73, 30, 72)])
    cfg = _cfg(tmp_path, "paper")
    st = State()
    report = Runner(cfg, kal, FakeNWS()).run_cycle(st)
    assert len(report.executed) == 2 and kal.orders == []
    assert (tmp_path / "state.json").exists()
    # second cycle: nothing new, positions marked, same tickers not re-bought
    report2 = Runner(cfg, kal, FakeNWS()).run_cycle(State.load(cfg.state_path))
    assert report2.executed == [] and len(report2.skipped) == 2


def test_runner_live_cycle_places_orders(tmp_path):
    kal = FakeKalshi([_mk("KXHIGHNY-X-B74.5", 74, 75, 20, 82)])
    cfg = _cfg(tmp_path, "live")
    report = Runner(cfg, kal, FakeNWS()).run_cycle(State())
    assert len(report.executed) == 1 and kal.orders[0][:2] == ("KXHIGHNY-X-B74.5", "yes")


def test_runner_settles_positions(tmp_path):
    m = _mk("KXHIGHNY-X-B74.5", 74, 75, 20, 82)
    kal = FakeKalshi([m])
    cfg = _cfg(tmp_path, "paper")
    st = State()
    Runner(cfg, kal, FakeNWS()).run_cycle(st)
    m["status"], m["result"] = "settled", "yes"
    st = State.load(cfg.state_path)
    n = st.positions["KXHIGHNY-X-B74.5"].count
    report = Runner(cfg, kal, FakeNWS()).run_cycle(st)
    assert report.settled and st.positions == {} and st.daily_realized_cents > 0
    assert st.daily_realized_cents == 100 * n - (n * 20 + __import__("kalshi_bot.fees").fees.taker_fee_cents(n, 20))
