"""One trading cycle: refresh marks, scan cities, size, execute, persist."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .config import Config
from .executor import Executor
from .kalshi_client import KalshiClient
from .nws import NWSClient
from .risk import RiskManager
from .state import State
from .strategy.weather import Candidate, WeatherStrategy

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    candidates: list[Candidate] = field(default_factory=list)
    executed: list[Candidate] = field(default_factory=list)
    skipped: list[tuple[Candidate, str]] = field(default_factory=list)
    settled: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    cancelled_orders: int = 0

    def summary(self, state: State) -> str:
        lines = [f"candidates={len(self.candidates)} executed={len(self.executed)} "
                 f"skipped={len(self.skipped)} settled={len(self.settled)} errors={len(self.errors)}",
                 f"open positions={len(state.positions)} exposure={state.open_exposure_cents()}c "
                 f"daily P&L={state.daily_pnl_cents():+}c (realized {state.daily_realized_cents:+}c)"]
        for c in self.executed:
            lines.append(f"  TRADED  {c}")
        for c, why in self.skipped[:10]:
            lines.append(f"  skip    {c.ticker} ({why})")
        for e in self.errors:
            lines.append(f"  ERROR   {e}")
        return "\n".join(lines)


class Runner:
    def __init__(self, cfg: Config, client: KalshiClient, nws: NWSClient,
                 strategy: WeatherStrategy | None = None, risk: RiskManager | None = None,
                 executor: Executor | None = None):
        self.cfg = cfg
        self.client = client
        self.nws = nws
        self.strategy = strategy or WeatherStrategy(cfg.strategy)
        self.risk = risk or RiskManager(cfg.risk)
        self.executor = executor or Executor(client, live=cfg.is_live)

    # ------------------------------------------------------------- scanning
    def scan(self) -> tuple[list[Candidate], dict[str, dict], list[str]]:
        """Return (candidates, markets-by-ticker, errors) across all cities."""
        candidates: list[Candidate] = []
        by_ticker: dict[str, dict] = {}
        errors: list[str] = []
        for city in self.cfg.cities:
            try:
                markets = self.client.get_markets(series_ticker=city.series)
                by_ticker.update({m["ticker"]: m for m in markets})
                highs = self.nws.daily_highs_f(city.lat, city.lon, city.tz)
                cands = self.strategy.evaluate(city, markets, highs)
                log.info("%-12s markets=%d forecast=%s candidates=%d", city.name, len(markets),
                         {d.isoformat(): round(t) for d, t in sorted(highs.items())[:3]}, len(cands))
                candidates.extend(cands)
            except Exception as exc:
                errors.append(f"{city.name}: {exc}")
                log.exception("scan failed for %s", city.name)
        candidates.sort(key=lambda c: c.edge_cents, reverse=True)
        return candidates, by_ticker, errors

    # --------------------------------------------------------- mark/settle
    def refresh_positions(self, state: State, by_ticker: dict[str, dict], report: CycleReport) -> None:
        for ticker, pos in list(state.positions.items()):
            m = by_ticker.get(ticker)
            if m is None:
                try:
                    m = self.client.get_market(ticker)
                except Exception as exc:
                    report.errors.append(f"mark {ticker}: {exc}")
                    continue
            result = (m.get("result") or "").lower()
            if m.get("status") in ("settled", "finalized") or result in ("yes", "no"):
                payout = 100 * pos.count if result == pos.side else 0
                state.settle(ticker, payout)
                report.settled.append(f"{ticker} result={result or '?'} payout={payout}c")
                continue
            bid = m.get(f"{pos.side}_bid")
            if bid:
                pos.last_mark_cents = int(bid)

    # ----------------------------------------------------------------- run
    def run_cycle(self, state: State, dry_run: bool = False) -> CycleReport:
        report = CycleReport()
        state.roll_day()
        if self.cfg.is_live and not dry_run:
            status = self.client.exchange_status()
            if not status.get("trading_active", True):
                report.errors.append(f"exchange not active: {status}")
                return report
            report.cancelled_orders = self.executor.cancel_stale_orders()

        candidates, by_ticker, errors = self.scan()
        report.candidates = candidates
        report.errors.extend(errors)
        self.refresh_positions(state, by_ticker, report)

        if dry_run:
            return report
        trades = 0
        for cand in candidates:
            sizing = self.risk.check(cand, state, trades)
            if not sizing.ok:
                report.skipped.append((cand, sizing.reason))
                continue
            try:
                if self.executor.execute(cand, sizing, state):
                    report.executed.append(cand)
                    trades += 1
            except Exception as exc:
                report.errors.append(f"execute {cand.ticker}: {exc}")
                log.exception("execute failed")
        state.save(self.cfg.state_path)
        return report
