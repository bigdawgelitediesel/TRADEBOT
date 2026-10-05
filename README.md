# TRADEBOT — Kalshi weather bot

Trades Kalshi's daily **high-temperature** markets by pricing each bucket off the
National Weather Service forecast and buying whichever side is mispriced after fees.
Ships with a paper mode, Kalshi demo-exchange support, and hard risk limits.

> This is not a guaranteed money-maker. Kalshi charges a fee on every fill and other
> bots trade the same public forecasts. Run it on **demo + paper** first, read the
> logs, then decide.

## How it works (one cycle)

1. Pull the NWS gridpoint forecast for each configured city → daily high in °F.
2. Pull open markets for that city's series (e.g. `KXHIGHNY`).
3. Model the settled high as Normal(forecast, σ) with σ widening by lead day.
4. For each bucket, P(YES) = area under the curve; EV = P×100 − ask − fee.
5. Keep candidates whose EV ≥ `min_edge_cents`, sort by edge.
6. Run each through the risk manager (per-trade cost, exposure cap, daily loss cap,
   one bucket per city-day, max trades per cycle, kill switch).
7. Paper: record a simulated fill. Live: place a limit order at the ask on Kalshi.
8. Mark open positions to the bid, settle finished markets, save `state/state.json`.

## Setup

```bash
pip install -r requirements.txt
```

Secrets go in the **environment**, never in the repo:

| variable | value |
|---|---|
| `KALSHI_API_KEY_ID` | key id from Kalshi → Account → API Keys |
| `KALSHI_PRIVATE_KEY` | full RSA private key PEM (or set `KALSHI_PRIVATE_KEY_PATH`) |
| `KALSHI_ENV` | `demo` (default) or `prod` |
| `TRADING_MODE` | `paper` (default) or `live` |

Kalshi only accepts **RSA** keys (`-----BEGIN RSA PRIVATE KEY-----`). The bot refuses
anything else with a clear error.

Outbound hosts needed: `api.elections.kalshi.com`, `demo-api.kalshi.co`, `api.weather.gov`.

## Commands

```bash
python -m kalshi_bot status      # exchange status, balance, Kalshi positions
python -m kalshi_bot keycheck    # does the key authenticate?
python -m kalshi_bot discover    # which weather series are open right now
python -m kalshi_bot scan        # show candidates, place nothing
python -m kalshi_bot run         # one cycle (paper or live per config)
python -m kalshi_bot loop -i 900 # a cycle every 15 min until Ctrl-C
python -m kalshi_bot positions   # bot-tracked positions and daily P&L
python -m kalshi_bot cancel-all  # pull every resting order (live)
```

## Going live, in order

1. `KALSHI_ENV=demo TRADING_MODE=paper` — paper on demo. Watch `scan` output for a few days.
2. `KALSHI_ENV=demo TRADING_MODE=live` — real orders, play money. Confirms auth + order flow.
3. `KALSHI_ENV=prod TRADING_MODE=paper` — real prices, no orders.
4. `KALSHI_ENV=prod TRADING_MODE=live` — real money. Start with the default $5/trade, $50 total.

**Emergency stop:** create a file named `STOP` in the repo root. The next cycle places nothing.
`python -m kalshi_bot cancel-all` pulls anything resting.

## Tuning (`config.json`)

- `strategy.min_edge_cents` — required EV per contract after fees. Raise to trade less, better.
- `strategy.sigma_by_lead_day` — forecast error in °F for today, tomorrow, … Bigger = more humble.
- `strategy.min_prob` / `max_prob` — ignore buckets the model thinks are near-certain either way.
- `risk.*` — cost caps, daily loss stop, max trades per cycle.
- `cities[]` — series ticker + the lat/lon of the **settlement station** (Central Park, Midway, …).
  Run `discover` to confirm the series tickers Kalshi currently lists.

## Tests

```bash
python -m pytest -q
```

All tests run offline (mocked Kalshi + NWS).
