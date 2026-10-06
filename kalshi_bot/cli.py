"""Command line entry point.

    python -m kalshi_bot status         exchange status, balance, open positions
    python -m kalshi_bot keycheck       verify the API key signs and authenticates
    python -m kalshi_bot discover       list open series/tickers that look like weather markets
    python -m kalshi_bot scan           show trade candidates, place nothing
    python -m kalshi_bot run            one full cycle (paper or live per config)
    python -m kalshi_bot loop -i 900    run a cycle every 900s until Ctrl-C
    python -m kalshi_bot positions      bot-tracked positions and daily P&L
    python -m kalshi_bot cancel-all     cancel every resting order (live only)
    python -m kalshi_bot btc 8 85203    in 8 minutes, above or below $85,203? (read-only)
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import Counter

from .config import Config, load_config
from .executor import Executor
from .kalshi_client import KalshiClient, KalshiError
from .nws import NWSClient
from .runner import Runner
from .state import State


def build(cfg: Config) -> tuple[KalshiClient, NWSClient]:
    client = KalshiClient(cfg.base_url, cfg.api_key_id, cfg.private_key_pem)
    return client, NWSClient()


def banner(cfg: Config) -> str:
    return (f"[kalshi-bot] env={cfg.env.upper()} mode={cfg.mode.upper()} "
            f"creds={'yes' if cfg.has_credentials else 'NO'} cities={len(cfg.cities)}")


def cmd_status(cfg: Config, client: KalshiClient) -> int:
    print(banner(cfg))
    print("exchange:", client.exchange_status())
    if cfg.has_credentials:
        bal = client.get_balance()
        print(f"balance: ${bal.get('balance', 0) / 100:.2f}  portfolio value: "
              f"${bal.get('portfolio_value', 0) / 100:.2f}")
        pos = client.get_positions().get("market_positions", [])
        live = [p for p in pos if p.get("position")]
        print(f"kalshi positions: {len(live)}")
        for p in live[:20]:
            print(f"  {p['ticker']:<28} pos={p['position']:+}  exposure={p.get('market_exposure', 0)}c")
    else:
        print("no credentials set -> public endpoints only")
    return 0


def cmd_keycheck(cfg: Config, client: KalshiClient) -> int:
    if not cfg.has_credentials:
        print("KALSHI_API_KEY_ID / KALSHI_PRIVATE_KEY not set in the environment")
        return 1
    try:
        bal = client.get_balance()
    except KalshiError as exc:
        print("auth FAILED:", exc)
        return 1
    print(f"auth OK on {cfg.env}. balance ${bal.get('balance', 0) / 100:.2f}")
    return 0


def cmd_discover(cfg: Config, client: KalshiClient) -> int:
    markets = client.get_markets(status="open", limit=1000, max_pages=5)
    series = Counter(m["ticker"].split("-")[0] for m in markets)
    weather = {s: n for s, n in series.items() if any(k in s for k in ("HIGH", "LOW", "TEMP", "RAIN", "SNOW"))}
    print(f"{len(markets)} open markets, {len(series)} series; weather-looking series:")
    for s, n in sorted(weather.items()):
        print(f"  {s:<16} {n} markets")
    configured = {c.series for c in cfg.cities}
    missing = configured - set(series)
    if missing:
        print("configured series with NO open markets right now:", ", ".join(sorted(missing)))
    return 0


def cmd_scan(cfg: Config, client: KalshiClient, nws: NWSClient) -> int:
    print(banner(cfg))
    state = State.load(cfg.state_path)
    runner = Runner(cfg, client, nws)
    report = runner.run_cycle(state, dry_run=True)
    print(f"{len(report.candidates)} candidates (nothing placed):")
    for c in report.candidates[:25]:
        print("  ", c)
    for e in report.errors:
        print("  ERROR", e)
    return 0


def cmd_run(cfg: Config, client: KalshiClient, nws: NWSClient) -> int:
    print(banner(cfg))
    if cfg.is_live and not cfg.has_credentials:
        print("live mode needs credentials"); return 1
    state = State.load(cfg.state_path)
    report = Runner(cfg, client, nws).run_cycle(state)
    print(report.summary(state))
    return 0


def cmd_loop(cfg: Config, client: KalshiClient, nws: NWSClient, interval: int) -> int:
    print(banner(cfg), f"interval={interval}s")
    runner = Runner(cfg, client, nws)
    while True:
        state = State.load(cfg.state_path)
        try:
            print(time.strftime("%Y-%m-%d %H:%M:%S"), runner.run_cycle(state).summary(state))
        except Exception as exc:
            logging.exception("cycle failed: %s", exc)
        time.sleep(interval)


def cmd_positions(cfg: Config) -> int:
    state = State.load(cfg.state_path)
    print(banner(cfg))
    print(f"day={state.day} trades today={state.daily_trades} realized={state.daily_realized_cents:+}c "
          f"unrealized={state.unrealized_cents():+}c exposure={state.open_exposure_cents()}c")
    for p in state.positions.values():
        tag = "paper" if p.paper else "LIVE"
        print(f"  {p.ticker:<28} {p.side.upper():<3} x{p.count:<3} @ {p.avg_price_cents:>4.1f}c "
              f"cost={p.cost_cents}c mark={p.last_mark_cents}c  [{tag}]")
    for t in state.trade_log[-10:]:
        print("  log:", t)
    return 0


def cmd_btc(args_rest: list[str]) -> int:
    from .btc import live_call
    if len(args_rest) != 2:
        print("usage: btc <minutes> <strike>   e.g.  btc 8 85203"); return 1
    minutes = float(args_rest[0].lower().rstrip("minsh").rstrip("m"))
    if args_rest[0].lower().endswith("h"):
        minutes *= 60
    strike = float(args_rest[1].replace(",", "").lstrip("$"))
    print(live_call(minutes, strike).render())
    return 0


def cmd_cancel_all(cfg: Config, client: KalshiClient) -> int:
    n = Executor(client, live=True).cancel_stale_orders()
    print(f"cancelled {n} resting orders on {cfg.env}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kalshi_bot", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["status", "keycheck", "discover", "scan", "run", "loop",
                                        "positions", "cancel-all", "btc"])
    ap.add_argument("rest", nargs="*", help="arguments for btc: <minutes> <strike>")
    ap.add_argument("-i", "--interval", type=int, default=900, help="loop interval seconds")
    ap.add_argument("-c", "--config", default=None, help="path to config.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if args.command == "btc":
        return cmd_btc(args.rest)

    cfg = load_config(args.config)
    client, nws = build(cfg)
    try:
        if args.command == "status":
            return cmd_status(cfg, client)
        if args.command == "keycheck":
            return cmd_keycheck(cfg, client)
        if args.command == "discover":
            return cmd_discover(cfg, client)
        if args.command == "scan":
            return cmd_scan(cfg, client, nws)
        if args.command == "run":
            return cmd_run(cfg, client, nws)
        if args.command == "loop":
            return cmd_loop(cfg, client, nws, args.interval)
        if args.command == "positions":
            return cmd_positions(cfg)
        if args.command == "cancel-all":
            return cmd_cancel_all(cfg, client)
    except KalshiError as exc:
        print("Kalshi error:", exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
