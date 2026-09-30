"""Run one deterministic FX day with a fake clock and no external network calls.

Usage: python tests/run_fx_day_simulation.py
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from atrade import engine, market_fx, state as state_mod, telegram
from deploy import dispatch_fx

ET = market_fx.ET


def fake_summary() -> dict:
    day = datetime(2026, 9, 28, 8, 30, tzinfo=ET)
    start = day.date() - timedelta(days=24)
    bars = []
    for index in range(24):
        close = 4050.0 + index * 5.0
        low, high = close - 28.0, close + 28.0
        if index == 18:
            low = 3900.0  # latest confirmed 3-bar swing low
        bars.append({"t": (start + timedelta(days=index)).isoformat(),
                     "o": close - 2, "h": high, "l": low, "c": close, "v": 1000})
    last = bars[-1]
    return {
        "asof": day.isoformat(),
        "notes": [{"category": "commodities", "tickers": ["XAUUSD"],
                   "title": "Gold demand firms", "summary": "Safe-haven demand and softer real yields support gold.",
                   "direction": "bullish", "strength": 0.95, "source": "fake-clock",
                   "date": day.date().isoformat()}],
        "prices": {"XAUUSD=X": {"symbol": "XAUUSD=X", "date": last["t"],
                                 "close": last["c"], "prev_close": last["c"] - 35,
                                 "chg_pct": 35 / (last["c"] - 35)}},
        "bars": {"XAUUSD=X": bars}, "fred": {}, "events": [], "dynamic": {},
    }


class SimEngine:
    """Inject canned research into the real FX engine functions."""
    def __init__(self, summary):
        self.summary = summary

    def fx_tick(self, state_dir, **kwargs):
        return engine.fx_tick(state_dir, **kwargs)

    def swing_open_run(self, state_dir, **kwargs):
        return engine.swing_open_run(state_dir, summary=self.summary, **kwargs)

    def risk_check_run(self, state_dir, **kwargs):
        return engine.risk_check_run(state_dir, summary=self.summary, **kwargs)

    def session_preview_run(self, state_dir, **kwargs):
        kwargs.pop("summary", None)
        return engine.session_preview_run(state_dir, summary=self.summary, **kwargs)

    def fx_week_ahead_run(self, state_dir, **kwargs):
        kwargs.pop("summary", None)
        return engine.fx_week_ahead_run(state_dir, summary=self.summary, **kwargs)

    def fx_open_positions(self, state_dir):
        return engine.fx_open_positions(state_dir)

    def fx_had_entry(self, state_dir, fx_day):
        return engine.fx_had_entry(state_dir, fx_day)


def main() -> int:
    summary = fake_summary()
    fake_engine = SimEngine(summary)
    with tempfile.TemporaryDirectory(prefix="atrade-fx-day-") as temp:
        state_dir = Path(temp) / "state_fx"
        state_dir.mkdir()
        (state_dir / "config.json").write_text(json.dumps({
            "risk_pct": 0.01,
            "cross_book_cluster_check": False,
            "mock_drift_scale": 0.0,
            "slippage_bps": 0.0,
            "spread": 0.30,
        }))
        state_mod.State(state_dir)  # seed the isolated state schema
        print("XAUUSD PAPER BOOK — SIMULATED FX DAY")
        print("Monday 2026-09-28 ET · fake clock · deterministic research/quotes · no live broker\n")

        sequence = [
            ("03:15", datetime(2026, 9, 28, 3, 15, tzinfo=ET), 4180.50),
            ("08:30", datetime(2026, 9, 28, 8, 30, tzinfo=ET), 4200.00),
            ("14:15", datetime(2026, 9, 28, 14, 15, tzinfo=ET), 4210.00),
            ("17:00", datetime(2026, 9, 28, 17, 0, tzinfo=ET), None),
            ("18:05", datetime(2026, 9, 28, 18, 5, tzinfo=ET), 4215.00),
        ]
        open_result = risk_result = preview_result = None
        tick_results = []
        for label, now, price in sequence:
            # Engine logging uses wall-clock timestamps; suppress incidental logs
            # so the captured artifact remains entirely fake-clocked.
            with contextlib.redirect_stdout(io.StringIO()):
                result = dispatch_fx.dispatch_tick(
                    state_dir, now=now, price=price, engine_api=fake_engine,
                    notify=False, sync_playbook=False)
            tick_results.append(result)
            print(f"{label} ET | FX day {result['fx_day']} | {result['session']} | "
                  f"tick={result['tick'].get('status')}")
            for action in result["actions"]:
                if action.get("status") == "error":
                    raise AssertionError(f"simulated dispatch failed: {action}")
                print(f"  {action.get('dispatch_name')} "
                      f"{'(catch-up) ' if action.get('catchup') else ''}→ {action.get('status')}")
                if action.get("dispatch_name") == "open":
                    open_result = action
                elif action.get("dispatch_name") == "checkin_am":
                    risk_result = action
                elif action.get("dispatch_name") == "checkin_pm":
                    risk_result = action
                elif action.get("dispatch_name") == "preview":
                    preview_result = action

        stored = json.loads((state_dir / "state.json").read_text())
        open_trades = [trade for trade in stored["ledger"] if trade.get("status") == "open"]
        if len(open_trades) != 1:
            raise AssertionError(f"expected one swing position held across the rollover, got {len(open_trades)}")
        trade = open_trades[0]
        if not any(result["fx_day"] == "2026-09-29" and result["session"] == "break"
                   for result in tick_results):
            raise AssertionError("fake clock did not cross the FX-day rollover break")

        print("\nCLOSE CHECK: no scheduled daily flatten ran; position remains OPEN across 17:00 ET.")
        print(f"  {trade['side'].upper()} {trade['qty_oz']:.3f} oz · entry ${trade['entry_price']:.2f} · "
              f"stop ${trade['stop_price']:.2f} · trail ${float(trade.get('trail_price') or 0):.2f}")
        print(f"  initial risk ${trade['risk_usd']:.2f} · ledger status={trade['status']} · "
              f"FX-day label={trade['fx_day']}")

        print("\n--- Rendered Telegram: swing open ---")
        if not open_result or not open_result.get("opened"):
            raise AssertionError("simulated day did not create the swing-open message sample")
        print(telegram.format_swing_open(
            trade["opened_at"], [trade], [], fx_day=trade["fx_day"],
            session="overlap", atr=trade["atr_24h"]))

        print("\n--- Rendered Telegram: risk check ---")
        if not risk_result:
            raise AssertionError("simulated day did not produce an FX risk check")
        print(telegram.format_risk_check(
            risk_result.get("last_risk_check_fx", {}).get("at") or
            datetime(2026, 9, 28, 14, 15, tzinfo=ET).isoformat(),
            risk_result.get("positions") or [], risk_result.get("updates") or [],
            ["No scheduled high-impact release flagged"], fx_day="2026-09-28",
            block="checkin_pm", quote_available=True))

        print("\n--- Rendered Telegram: session preview ---")
        if not preview_result:
            raise AssertionError("simulated day did not produce the session preview")
        preview = preview_result
        print(telegram.format_session_preview(
            datetime(2026, 9, 28, 18, 5, tzinfo=ET).isoformat(),
            "asia", preview.get("events") or [], preview.get("hypotheses") or [],
            engine._fx_positions(engine._fx_context(state_dir)[0], 4215.0),
            paused=False, price=4215.0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
