"""Configuration: package defaults + runtime overrides stored in state dir."""
from __future__ import annotations

import json
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent.parent  # .../atrade/

DEFAULTS = {
    # --- execution / broker -----------------------------------------------
    "broker": "alpaca",          # "alpaca" (paper) or "mock"
    "alpaca_paper": True,        # NEVER switch to live
    "initial_equity": 100000.0,  # paper starting equity reference
    "slippage_bps": 2.0,         # simulated slippage for mock fills
    # --- scheduling --------------------------------------------------------
    "open_run_time_et": "09:25",   # research + open new trades before the bell
    "close_run_time_et": "15:50",  # close day trades, evaluate, learn
    "early_close_time_et": "12:50",# early-close days (e.g. day after Thanksgiving)
    "weekdays_only": True,
    "timezone": "America/New_York",
    # --- strategy ------------------------------------------------------------
    "min_confidence": 0.60,        # only open trades with hypothesis confidence >= 60%
    "max_positions": 2,            # concurrent day trades
    "max_position_pct": 0.12,      # 12% of equity per position at confidence 0.80+
    "conf_to_size": {0.60: 0.06, 0.70: 0.09, 0.80: 0.12},  # interpolated
    "max_portfolio_pct": 0.24,     # max total deployed
    "day_trade_only": True,        # all positions closed at close run
    "min_trade_value": 200.0,
    "max_trade_value": 20000.0,
    # --- evaluation ----------------------------------------------------------
    "weights": {"return": 0.35, "alpha": 0.30, "sharpe": 0.20, "win_rate": 0.15},
    "pause_no_improve_streak": 6,   # pause after 6 consecutive runs w/o improvement
    "pause_failed_measurements": 3, # pause after 3 consecutive failed measurements
    # --- warm-up (anti-overfitting) -------------------------------------------
    # The first `warmup_until_trades` trades are sized at 40%..100% of normal,
    # scaling up as the signal tracker accumulates real samples. Prevents early
    # learning on tiny sample sizes from costing meaningful size.
    "warmup_until_trades": 12,
    "warmup_min_factor": 0.40,
    # --- research -------------------------------------------------------------
    "universe": [
        "SPY", "QQQ", "IWM", "AAPL", "MSFT", "NVDA", "TSLA", "AMD", "META",
        "AMZN", "GOOGL", "JPM", "XOM", "CVX", "GLD", "SLV", "USO", "TLT",
        "XLE", "XLF", "XLK", "XLY", "XLV", "XLI", "XLB", "XLU", "XLP", "XLC",
        "PLTR", "ORCL",
    ],
    "rss_queries": [
        "Federal Reserve interest rates",
        "CPI inflation report",
        "stock market today",
        "earnings surprise",
        "crude oil OPEC",
        "gold price",
        "AI chip stocks",
    ],
    "fred_series": ["DGS10", "DGS2", "FEDFUNDS", "UNRATE", "CPIAUCSL", "PPIFID"],
    "sec_enabled": True,
    "sec_user_agent": "TradingResearchAgent/1.0 (educational paper-trading research; contact: agent@example.com)",
}

# Runtime overrides file lives next to the state so different state dirs
# can carry different config (e.g. demo vs live).


def _state_dir() -> Path:
    env = os.environ.get("ATRADE_STATE_DIR")
    if env:
        return Path(env)
    return PACKAGE_DIR / "state"


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    over = _state_dir() / "config.json"
    if over.exists():
        try:
            cfg.update(json.loads(over.read_text()))
        except Exception:
            pass
    return cfg


def save_config_override(cfg: dict) -> None:
    from atrade import util

    util.write_json(_state_dir() / "config.json", cfg)


def load_env_keys() -> dict:
    """Read Alpaca keys from .env (KEY=value lines) — never from live trading endpoints."""
    keys = {}
    for p in [PACKAGE_DIR / ".env", PACKAGE_DIR / ".env.example"]:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            keys[k.strip()] = v.strip().strip('"').strip("'")
    # environment variables take precedence
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER",
          "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
    if os.environ.get(k):
        keys[k] = os.environ[k]
    return keys
