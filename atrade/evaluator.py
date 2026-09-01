"""Evaluator: composite performance score, baseline capture, pause logic.

Composite = 0.35*Return + 0.30*Alpha(vs SPY) + 0.20*Sharpe + 0.15*WinRate
The baseline (SPY buy&hold over the same window) is captured at setup and
frozen — every run is graded against it by the "independent evaluator"
(a fresh calculation from ledger + benchmark history, not the agent's memory).

Pause conditions (auto):
  - 6 consecutive runs with no composite improvement
  - 3 consecutive failed measurements (missing benchmark data etc.)
"""
from __future__ import annotations

import math
from datetime import date

from . import market, util


def _eq(ledger: list[dict]) -> dict:
    """Return total equity from ledger closed P&L (starting from initial equity)."""
    cash = 0.0
    for t in ledger:
        if t.get("pnl") is not None:
            cash += t["pnl"]
    return cash  # NET p&l; caller adds starting equity


def compute_metrics(ledger: list[dict], start_equity: float, current_equity: float | None,
                    benchmark_pct: float | None, start_date: str | None) -> dict:
    """Compute portfolio metrics. All percentage metrics as decimals."""
    closed = [t for t in ledger if t.get("pnl") is not None]
    net_pnl = sum(t["pnl"] for t in closed)
    if current_equity is None or current_equity <= 0:
        current_equity = start_equity + net_pnl
    port_ret = current_equity / start_equity - 1.0 if start_equity else None

    alpha = None
    if port_ret is not None and benchmark_pct is not None:
        alpha = port_ret - benchmark_pct

    # Sharpe: realized per-trade returns, annualized assuming ~1 trade/day avg
    sharpe = None
    rets = [t.get("pnl_pct") for t in closed if isinstance(t.get("pnl_pct"), (int, float))]
    if len(rets) >= 3:
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var) if var > 0 else 0.0
        if std > 0:
            sharpe = round((mean / std) * math.sqrt(252.0 / len(rets)), 3)

    wins = [r for r in rets if r > 0]
    win_rate = len(wins) / len(rets) if rets else None

    return {"net_pnl": round(net_pnl, 2), "portfolio_return": port_ret,
            "benchmark_return": benchmark_pct, "alpha": alpha,
            "sharpe": round(sharpe, 3) if sharpe is not None else None,
            "win_rate": win_rate, "n_trades": len(closed),
            "n_open": len([t for t in ledger if t.get("status") == "open"])}


def composite_score(m: dict, weights: dict | None = None) -> tuple[float, dict]:
    """Weighted composite in [0,1]. If a component is unavailable (e.g. Sharpe
    with <3 closed trades), it is excluded and remaining weights renormalized —
    early runs with few trades must still be gradeable."""
    w = dict(weights or {"return": 0.35, "alpha": 0.30, "sharpe": 0.20, "win_rate": 0.15})
    components = {}
    total = 0.0
    avail_weight = 0.0

    def part(key, val, min_v, max_v):
        nonlocal total, avail_weight
        if val is None or not math.isfinite(float(val)):
            components[key] = None
            return
        norm = max(0.0, min(1.0, (float(val) - min_v) / (max_v - min_v)))
        total += w[key] * norm
        avail_weight += w[key]
        components[key] = round(norm, 3)

    part("return", m.get("portfolio_return"), -0.05, 0.05)
    part("alpha", m.get("alpha"), -0.03, 0.03)
    part("sharpe", m.get("sharpe"), 0.0, 2.0)
    part("win_rate", m.get("win_rate"), 0.0, 0.75)
    if avail_weight <= 0:
        return 0.0, components
    return round(total / avail_weight, 4), components


def capture_baseline(price_map: dict, config_dir, equity: float) -> dict:
    """Capture the frozen benchmark baseline at setup."""
    spy = price_map.get("SPY") or price_map.get("spy")
    qqq = price_map.get("QQQ") or price_map.get("qqq")
    base = {
        "captured_at": util.utc_iso(),
        "initial_equity": equity,
        "benchmarks": {
            "SPY": {"price": spy},
            "QQQ": {"price": qqq},
        },
        "note": "Frozen baseline: every run is graded vs SPY buy&hold from this point.",
    }
    return base


def evaluate_run(state: dict, cfg: dict, equity_now: float | None, benchmark_pct: float | None) -> dict:
    """Full evaluation for one run: metrics + composite + pause flags."""
    s = state
    ledger = s.get("ledger", [])
    base = s.get("baseline") or {}
    start_equity = base.get("initial_equity") or cfg.get("initial_equity", 100000.0)
    m = compute_metrics(ledger, start_equity, equity_now, benchmark_pct, base.get("captured_at"))
    score, comps = composite_score(m, cfg.get("weights"))
    history = s.get("score_history", [])
    prev_scores = [h["score"] for h in history if h.get("score") is not None]
    best = max(prev_scores) if prev_scores else None
    improved = (best is None) or (score > best)
    s.setdefault("streaks", {})
    if best is None or score > best:
        s["streaks"]["no_improve"] = 0
    else:
        s["streaks"]["no_improve"] = s["streaks"].get("no_improve", 0) + 1
    # failed measurement streak
    failed = False
    if m.get("n_trades", 0) > 0:
        failed = m.get("benchmark_return") is None or m.get("portfolio_return") is None or score == 0.0
    if failed:
        s["streaks"]["failed_measure"] = s["streaks"].get("failed_measure", 0) + 1
    else:
        s["streaks"]["failed_measure"] = 0
    pause = (s["streaks"].get("no_improve", 0) >= cfg.get("pause_no_improve_streak", 6) or
             s["streaks"].get("failed_measure", 0) >= cfg.get("pause_failed_measurements", 3))
    return {
        "metrics": m, "score": score, "components": comps,
        "best_score": best, "improved": improved,
        "no_improve_streak": s["streaks"].get("no_improve", 0),
        "failed_measure_streak": s["streaks"].get("failed_measure", 0),
        "pause": pause,
    }


def benchmark_return(price_map: dict, base: dict) -> float | None:
    """SPY return from baseline capture to now."""
    spy0 = (base.get("benchmarks") or {}).get("SPY", {}).get("price")
    spy1 = price_map.get("SPY") or price_map.get("spy")
    if spy0 and spy1:
        return spy1 / spy0 - 1.0
    return None
