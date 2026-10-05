"""Order execution: paper (simulated fills) or live (real Kalshi orders)."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .kalshi_client import KalshiClient
from .risk import Sizing
from .state import Position, State
from .strategy.weather import Candidate

log = logging.getLogger(__name__)


class Executor:
    def __init__(self, client: KalshiClient | None, live: bool):
        self.client = client
        self.live = live
        if live and client is None:
            raise ValueError("live mode requires an authenticated client")

    def execute(self, cand: Candidate, sizing: Sizing, state: State) -> Position | None:
        now = datetime.now(timezone.utc).isoformat()
        if not self.live:
            pos = Position(ticker=cand.ticker, event_ticker=cand.event_ticker, side=cand.side,
                           count=sizing.count, avg_price_cents=cand.price_cents,
                           cost_cents=sizing.cost_cents, opened_at=now, paper=True,
                           last_mark_cents=cand.price_cents)
            state.record_fill(pos, note="paper fill at ask")
            log.info("PAPER  %s", cand)
            return pos

        assert self.client is not None
        order = self.client.place_order(ticker=cand.ticker, side=cand.side,
                                        count=sizing.count, price_cents=cand.price_cents)
        filled = int(order.get("fill_count") or order.get("count") or sizing.count)
        status = order.get("status", "?")
        log.info("LIVE   %s -> order %s status=%s filled=%s",
                 cand, order.get("order_id"), status, filled)
        if filled <= 0:
            return None
        pos = Position(ticker=cand.ticker, event_ticker=cand.event_ticker, side=cand.side,
                       count=filled, avg_price_cents=cand.price_cents,
                       cost_cents=sizing.cost_cents, opened_at=now, paper=False,
                       last_mark_cents=cand.price_cents)
        state.record_fill(pos, note=f"live order {order.get('order_id')} status={status}")
        return pos

    def cancel_stale_orders(self) -> int:
        """Cancel any resting orders so we never leave stale quotes on the book."""
        if not self.live or self.client is None:
            return 0
        n = 0
        for o in self.client.get_orders(status="resting"):
            try:
                self.client.cancel_order(o["order_id"])
                n += 1
            except Exception as exc:  # keep going; report at the end
                log.warning("cancel %s failed: %s", o.get("order_id"), exc)
        return n
