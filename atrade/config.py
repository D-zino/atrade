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
    "max_positions": 5,            # concurrent day trades
    "max_position_pct": 0.15,      # 15% of equity per position at confidence 0.80+
    "conf_to_size": {0.60: 0.09, 0.70: 0.12, 0.80: 0.15},  # interpolated
    "max_portfolio_pct": 0.60,     # max total deployed
    "day_trade_only": True,        # all positions closed at close run
    "min_trade_value": 200.0,
    "max_trade_value": 80000.0,
    # --- risk / loss limits ----------------------------------------------------
    "intraday_stop_pct": 0.02,      # per-position defensive stop (2% adverse)
    "daily_loss_limit_pct": 0.04,    # halt NEW trades for the day if today's P&L
                                     # (realized + unrealized) falls below -3% of equity
    "max_drawdown_pct": 0.14,        # auto-pause if equity drops 14% below its peak
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
    # --- dynamic universe (Tier 2: news discovery + momentum rotation) ---------
    "auto_dynamic_universe": True,   # enrich the scan set from news/SEC + momentum
    "max_dynamic_additions": 12,     # max extra symbols scanned per run
    "momentum_top_n": 6,             # top N momentum/volume movers added to scan
    "rotation_bias_top": 3,          # top N sector ETFs get a small confidence boost
    "min_dynamic_volume": 500000,    # ignore candidates with avg volume below this
    # liquid candidate pool the scanner can pull from (news mentions / momentum)
    "candidate_pool": [
        "AVGO", "NFLX", "CRM", "ADBE", "MU", "QCOM", "INTC", "ORCL", "PLTR",
        "COIN", "SHOP", "SNOW", "DDOG", "CRWD", "PANW", "NOW", "UBER", "ABNB",
        "WMT", "JNJ", "PG", "KO", "PEP", "BA", "DIS", "GE", "CAT", "HON", "UNH",
        "LLY", "PFE", "MRK", "ABBV", "TMO", "V", "MA", "AXP", "GS", "BAC", "WFC",
        "KO", "XOM", "CVX", "T", "VZ", "INTC", "AMD", "TSM", "BABA", "PDD",
        "SMH", "SOXX", "ARKK", "XBI", "IGV", "ITB", "KRE", "XLRE", "DIA",
        "UNG", "UUP", "VIXY", "FXY",
    ],
    # sector ETF each symbol belongs to (for rotation bias + clustering)
    "sector_of": {
        "NVDA": "XLK", "AMD": "XLK", "MU": "XLK", "AVGO": "XLK", "QCOM": "XLK",
        "INTC": "XLK", "SMH": "XLK", "SOXX": "XLK", "AAPL": "XLK", "MSFT": "XLK",
        "META": "XLK", "AMZN": "XLY", "GOOGL": "XLK", "TSLA": "XLY", "NFLX": "XLY",
        "CRM": "XLK", "ADBE": "XLK", "PLTR": "XLK", "ORCL": "XLK", "COIN": "XLK",
        "SHOP": "XLK", "SNOW": "XLK", "DDOG": "XLK", "CRWD": "XLK", "PANW": "XLK",
        "NOW": "XLK", "UBER": "XLY", "ABNB": "XLY",
        "JPM": "XLF", "GS": "XLF", "BAC": "XLF", "WFC": "XLF", "AXP": "XLF",
        "V": "XLF", "MA": "XLF", "XLF": "XLF", "KRE": "XLF",
        "XOM": "XLE", "CVX": "XLE", "XLE": "XLE", "USO": "XLE", "UNG": "XLE",
        "GLD": "GLD", "SLV": "SLV", "TLT": "TLT", "UUP": "UUP", "VIXY": "VIXY",
        "WMT": "XLP", "PG": "XLP", "KO": "XLP", "PEP": "XLP", "XLP": "XLP",
        "JNJ": "XLV", "UNH": "XLV", "LLY": "XLV", "PFE": "XLV", "MRK": "XLV",
        "ABBV": "XLV", "TMO": "XLV", "XLV": "XLV", "XBI": "XLV",
        "BA": "XLI", "DIS": "XLI", "GE": "XLI", "CAT": "XLI", "HON": "XLI", "XLI": "XLI",
        "T": "XLC", "VZ": "XLC", "XLC": "XLC", "META": "XLC",
        "XLY": "XLY", "XLK": "XLK", "XLB": "XLB", "XLU": "XLU", "XLRE": "XLRE",
        "IWM": "IWM", "SPY": "SPY", "QQQ": "QQQ", "DIA": "DIA", "FXY": "FXY",
        "XLB": "XLB", "XLB": "XLB",
    },
    # correlated clusters — never stack too many positions in one cluster
    "correlation_clusters": {
        "semis": ["NVDA", "AMD", "MU", "AVGO", "QCOM", "INTC", "SMH", "SOXX", "TSM"],
        "bigtech_software": ["MSFT", "GOOGL", "META", "CRM", "ADBE", "PLTR", "ORCL",
                             "SNOW", "DDOG", "CRWD", "PANW", "NOW", "SHOP"],
        "consumer_tech": ["AMZN", "TSLA", "NFLX", "UBER", "ABNB"],
        "financials": ["JPM", "GS", "BAC", "WFC", "AXP", "V", "MA", "KRE"],
        "energy": ["XOM", "CVX", "USO", "XLE", "UNG"],
        "health": ["JNJ", "UNH", "LLY", "PFE", "MRK", "ABBV", "TMO", "XLV", "XBI"],
        "staples": ["WMT", "PG", "KO", "PEP", "XLP"],
        "industrials": ["BA", "DIS", "GE", "CAT", "HON", "XLI"],
        "comm": ["T", "VZ", "XLC"],
        "precious_metals": ["GLD", "SLV"],
        "rates": ["TLT", "UUP", "VIXY", "FXY"],
    },
    "max_per_cluster": 2,          # max concurrent positions in one correlated cluster
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
    # environment variables take precedence (GitHub Actions secrets land here)
    for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_PAPER",
              "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if os.environ.get(k):
            keys[k] = os.environ[k]
    return keys
