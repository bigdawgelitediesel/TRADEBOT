"""Hard risk limits. Every order passes through `RiskManager.check` first."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import ROOT, RiskConfig
from .fees import taker_fee_cents
from .state import State
from .strategy.weather import Candidate


@dataclass
class Sizing:
    count: int
    cost_cents: int      # including fee
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.count > 0


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def kill_switch_engaged(self) -> bool:
        return (ROOT / self.cfg.kill_switch_file).exists() or Path(self.cfg.kill_switch_file).exists()

    def check(self, cand: Candidate, state: State, trades_this_cycle: int) -> Sizing:
        c = self.cfg
        if self.kill_switch_engaged():
            return Sizing(0, 0, f"kill switch file '{c.kill_switch_file}' present")
        if state.daily_pnl_cents() <= -c.max_daily_loss_cents:
            return Sizing(0, 0, f"daily loss limit hit ({state.daily_pnl_cents()}c)")
        if trades_this_cycle >= c.max_trades_per_cycle:
            return Sizing(0, 0, "max trades this cycle")
        if cand.ticker in state.positions:
            return Sizing(0, 0, "already hold this market")
        if state.positions_in_event(cand.event_ticker) >= c.max_positions_per_event:
            return Sizing(0, 0, "already hold a bucket in this event")

        count = min(c.max_contracts_per_trade, c.max_cost_per_trade_cents // cand.price_cents)
        room = c.max_open_exposure_cents - state.open_exposure_cents()
        count = min(count, room // cand.price_cents) if room > 0 else 0
        while count > 0:
            cost = count * cand.price_cents + taker_fee_cents(count, cand.price_cents)
            if cost <= c.max_cost_per_trade_cents and cost <= room:
                return Sizing(count, cost)
            count -= 1
        return Sizing(0, 0, "no room under per-trade / exposure limits")
