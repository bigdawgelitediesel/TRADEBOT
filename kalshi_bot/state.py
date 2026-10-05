"""Persistent bot state: open positions (cost basis), trade log, and daily P&L tally.

Stored as JSON so it survives between cycles. In live mode Kalshi is the source
of truth for positions; this file still records what the bot did and tracks the
daily loss limit.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path


@dataclass
class Position:
    ticker: str
    event_ticker: str
    side: str
    count: int
    avg_price_cents: float
    cost_cents: int             # contracts * price + fees
    opened_at: str
    paper: bool
    last_mark_cents: int | None = None

    @property
    def mark_value_cents(self) -> int:
        mark = self.last_mark_cents if self.last_mark_cents is not None else round(self.avg_price_cents)
        return int(self.count * mark)


@dataclass
class State:
    day: str = ""
    daily_realized_cents: int = 0
    daily_trades: int = 0
    positions: dict[str, Position] = field(default_factory=dict)   # key = ticker
    trade_log: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------ persistence
    @classmethod
    def load(cls, path: Path) -> "State":
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text())
        st = cls(day=raw.get("day", ""), daily_realized_cents=raw.get("daily_realized_cents", 0),
                 daily_trades=raw.get("daily_trades", 0), trade_log=raw.get("trade_log", []))
        st.positions = {k: Position(**v) for k, v in raw.get("positions", {}).items()}
        return st

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str))
        tmp.replace(path)

    # --------------------------------------------------------------- helpers
    def roll_day(self, today: date | None = None) -> None:
        today_s = (today or datetime.now(timezone.utc).date()).isoformat()
        if self.day != today_s:
            self.day = today_s
            self.daily_realized_cents = 0
            self.daily_trades = 0

    def open_exposure_cents(self) -> int:
        return sum(p.cost_cents for p in self.positions.values())

    def unrealized_cents(self) -> int:
        return sum(p.mark_value_cents - p.cost_cents for p in self.positions.values())

    def daily_pnl_cents(self) -> int:
        return self.daily_realized_cents + self.unrealized_cents()

    def positions_in_event(self, event_ticker: str) -> int:
        return sum(1 for p in self.positions.values() if p.event_ticker == event_ticker)

    def record_fill(self, pos: Position, note: str = "") -> None:
        self.positions[pos.ticker] = pos
        self.daily_trades += 1
        self.trade_log.append({
            "at": pos.opened_at, "ticker": pos.ticker, "side": pos.side, "count": pos.count,
            "price_cents": pos.avg_price_cents, "cost_cents": pos.cost_cents,
            "paper": pos.paper, "note": note,
        })
        self.trade_log = self.trade_log[-500:]

    def settle(self, ticker: str, payout_cents: int) -> None:
        pos = self.positions.pop(ticker, None)
        if pos is None:
            return
        self.daily_realized_cents += payout_cents - pos.cost_cents
        self.trade_log.append({
            "at": datetime.now(timezone.utc).isoformat(), "ticker": ticker, "settled": True,
            "payout_cents": payout_cents, "pnl_cents": payout_cents - pos.cost_cents,
        })
