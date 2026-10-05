"""Bot configuration: config.json for strategy/risk, environment variables for secrets.

Secrets are NEVER read from the repo. Set them in the environment:
    KALSHI_API_KEY_ID      - the key id shown in Kalshi -> Account -> API Keys
    KALSHI_PRIVATE_KEY     - full contents of the RSA private key file
                             (or KALSHI_PRIVATE_KEY_PATH pointing at the file)
Optional overrides:
    KALSHI_ENV             - "demo" (default) or "prod"
    TRADING_MODE           - "paper" (default) or "live"
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT / "config.json"

BASE_URLS = {
    "demo": "https://demo-api.kalshi.co/trade-api/v2",
    "prod": "https://api.elections.kalshi.com/trade-api/v2",
}


@dataclass
class City:
    name: str
    series: str          # Kalshi series ticker for the daily-high market, e.g. KXHIGHNY
    lat: float
    lon: float
    tz: str              # IANA zone, e.g. America/New_York


@dataclass
class StrategyConfig:
    min_edge_cents: int = 6            # required EV per contract after fees
    min_prob: float = 0.08             # ignore buckets our model thinks are near-impossible
    max_prob: float = 0.92             # ...or near-certain (nothing to earn, tail risk only)
    sigma_by_lead_day: list[float] = field(default_factory=lambda: [2.2, 3.0, 3.6, 4.2])
    min_hours_to_close: float = 3.0    # skip markets about to close
    max_lead_days: int = 2             # only trade today..today+N


@dataclass
class RiskConfig:
    max_contracts_per_trade: int = 10
    max_cost_per_trade_cents: int = 500       # $5
    max_open_exposure_cents: int = 5000       # $50 total cost basis across open positions
    max_daily_loss_cents: int = 1500          # stop trading for the day past -$15
    max_trades_per_cycle: int = 3
    max_positions_per_event: int = 1          # one bucket per city-day
    kill_switch_file: str = "STOP"            # touch this file in the repo root to halt


@dataclass
class Config:
    env: str = "demo"
    mode: str = "paper"
    cities: list[City] = field(default_factory=list)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    state_path: Path = ROOT / "state" / "state.json"
    api_key_id: str | None = None
    private_key_pem: str | None = None

    @property
    def base_url(self) -> str:
        return BASE_URLS[self.env]

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def has_credentials(self) -> bool:
        return bool(self.api_key_id and self.private_key_pem)


def _load_private_key() -> str | None:
    pem = os.environ.get("KALSHI_PRIVATE_KEY")
    if pem:
        # Secrets UIs often flatten newlines into the two characters "\n".
        return pem.replace("\\n", "\n").strip() + "\n"
    path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if path and Path(path).exists():
        return Path(path).read_text()
    return None


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw = json.loads(path.read_text()) if path.exists() else {}

    cfg = Config(
        env=os.environ.get("KALSHI_ENV", raw.get("env", "demo")).lower(),
        mode=os.environ.get("TRADING_MODE", raw.get("mode", "paper")).lower(),
        cities=[City(**c) for c in raw.get("cities", [])],
        strategy=StrategyConfig(**raw.get("strategy", {})),
        risk=RiskConfig(**raw.get("risk", {})),
        api_key_id=os.environ.get("KALSHI_API_KEY_ID"),
        private_key_pem=_load_private_key(),
    )
    if "state_path" in raw:
        cfg.state_path = ROOT / raw["state_path"]
    if cfg.env not in BASE_URLS:
        raise ValueError(f"env must be one of {list(BASE_URLS)}, got {cfg.env!r}")
    if cfg.mode not in ("paper", "live"):
        raise ValueError(f"mode must be 'paper' or 'live', got {cfg.mode!r}")
    return cfg
