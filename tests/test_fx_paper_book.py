from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from atrade import broker, engine, market_fx, trading
from deploy import dispatch_fx

ET = market_fx.ET


def et(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ET)


def fake_summary(now: datetime | None = None) -> dict:
    """Deterministic XAU bars/evidence; no network data is used by the harness."""
    now = now or et("2026-09-28T08:30:00")
    start = now.date() - timedelta(days=24)
    bars = []
    for index in range(24):
        close = 4050.0 + index * 5.0
        low = close - 28.0
        high = close + 28.0
        if index == 18:
            low = 3900.0  # confirmed 3-bar swing low
        bars.append({
            "t": (start + timedelta(days=index)).isoformat(),
            "o": close - 2.0, "h": high, "l": low, "c": close, "v": 1000,
        })
    last = bars[-1]
    return {
        "asof": now.isoformat(),
        "notes": [{
            "category": "commodities", "tickers": ["XAUUSD"],
            "title": "Gold demand firms",
            "summary": "Safe-haven flows and weaker real yields support gold.",
            "direction": "bullish", "strength": 0.95, "source": "fake-clock",
            "date": now.date().isoformat(),
        }],
        "prices": {
            "XAUUSD=X": {"symbol": "XAUUSD=X", "date": last["t"],
                         "close": last["c"], "prev_close": last["c"] - 35,
                         "chg_pct": 35 / (last["c"] - 35)},
        },
        "bars": {"XAUUSD=X": bars},
        "fred": {},
        "events": [],
        "dynamic": {},
    }


def make_fx_state(directory: str | Path) -> Path:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text(json.dumps({
        "risk_pct": 0.01,
        "cross_book_cluster_check": False,
        "mock_drift_scale": 0.0,
        "slippage_bps": 0.0,
        "spread": 0.30,
    }))
    return root


class DispatchStub:
    def __init__(self):
        self.calls = []
        self.positions = []
        self.entries = set()

    def fx_tick(self, state_dir, now=None, price=None, **kwargs):
        self.calls.append(("stop_tick", now))
        return {"status": "ok", "price": 4200.0, "source": "fake-clock"}

    def swing_open_run(self, state_dir, now=None, price=None, **kwargs):
        self.calls.append(("open", now, price))
        label = market_fx.fx_day(now).isoformat()
        self.entries.add(label)
        self.positions = [{"symbol": "XAUUSD", "side": "long", "status": "open"}]
        return {"status": "ok", "opened": []}

    def risk_check_run(self, state_dir, now=None, block=None, **kwargs):
        self.calls.append((block, now))
        return {"status": "ok", "positions": self.positions}

    def session_preview_run(self, state_dir, now=None, **kwargs):
        self.calls.append(("preview", now))
        return {"status": "ok"}

    def fx_week_ahead_run(self, state_dir, now=None, **kwargs):
        self.calls.append(("week_ahead", now))
        return {"status": "ok"}

    def fx_open_positions(self, state_dir):
        return self.positions

    def fx_had_entry(self, state_dir, fx_day):
        return fx_day in self.entries


class XAUUSDHelperTests(unittest.TestCase):
    def test_risk_sizing_and_confirmed_pivot_stop(self):
        bars = fake_summary()["bars"]["XAUUSD=X"]
        atr = trading.atr_24h(bars, 14)
        stop = trading.swing_initial_stop(4200.0, "long", bars, atr, 3.0)
        self.assertIsNotNone(stop)
        self.assertLess(stop["stop_price"], 4200.0)
        self.assertEqual(stop["stop_basis"], "3x_atr_tighter_than_pivot")
        qty = trading.swing_size(100000, 0.01, 4200, stop["stop_price"])
        self.assertGreater(qty, 0)
        self.assertLessEqual(qty * abs(4200 - stop["stop_price"]), 1000)
        self.assertEqual(trading.latest_confirmed_swing(bars, "long"), 3900.0)

    def test_fx_mock_supports_fractional_oz_spread_and_isolated_account(self):
        with tempfile.TemporaryDirectory() as temp:
            paper = broker.MockBroker(
                temp, initial_equity=100000, slippage_bps=0,
                price_src={"XAUUSD": 4200}, fractional_qty=True,
                spread=0.30, account_filename="mock_account_fx.json", drift_scale=0,
            )
            buy = paper.submit_order("XAUUSD", 2.125, "buy")
            self.assertAlmostEqual(float(buy["filled_avg_price"]), 4200.15, places=4)
            self.assertAlmostEqual(paper.positions()[0]["qty"], 2.125, places=3)
            self.assertTrue((Path(temp) / "mock_account_fx.json").exists())
            self.assertFalse((Path(temp) / "mock_account.json").exists())
            sell = paper.submit_order("XAUUSD", 2.125, "sell")
            self.assertAlmostEqual(float(sell["filled_avg_price"]), 4199.85, places=4)
            self.assertEqual(paper.positions(), [])

    def test_idle_flat_tick_does_not_write_state_or_markers(self):
        with tempfile.TemporaryDirectory() as temp:
            state = make_fx_state(temp)
            result = dispatch_fx.dispatch_tick(
                state, et("2026-09-28T01:00:00"),
                notify=False, sync_playbook=False)
            self.assertEqual(result["tick"]["status"], "flat")
            self.assertEqual(result["actions"], [])
            self.assertFalse((state / "state.json").exists())
            self.assertFalse((state / dispatch_fx.MARKER_NAME).exists())

    def test_dispatch_catchup_open_and_late_risk_check(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = DispatchStub()
            # 09:00 is inside the open window. The London check-in window was
            # missed; after opening, its bounded catch-up runs once.
            result = dispatch_fx.dispatch_tick(
                temp, et("2026-09-28T09:00:00"), engine_api=fake,
                notify=False, sync_playbook=False)
            self.assertEqual([call[0] for call in fake.calls], ["stop_tick", "open", "checkin_am"])
            check = result["marker"]["runs"]["checkin_am"]
            self.assertTrue(check["catchup"])
            # A later tick cannot loop the same block again.
            again = dispatch_fx.dispatch_tick(
                temp, et("2026-09-28T09:15:00"), engine_api=fake,
                notify=False, sync_playbook=False)
            self.assertEqual(sum(call[0] == "checkin_am" for call in fake.calls), 1)
            self.assertEqual(again["actions"], [])

    def test_missed_open_catches_up_at_1340_and_pm_catchup_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            fake = DispatchStub()
            at_1340 = dispatch_fx.dispatch_tick(
                temp, et("2026-09-28T13:40:00"), engine_api=fake,
                notify=False, sync_playbook=False)
            self.assertEqual([call[0] for call in fake.calls], ["stop_tick", "open"])
            self.assertTrue(at_1340["marker"]["runs"]["open"]["catchup"])

        with tempfile.TemporaryDirectory() as temp:
            fake = DispatchStub()
            fake.positions = [{"symbol": "XAUUSD", "side": "long", "status": "open"}]
            result = dispatch_fx.dispatch_tick(
                temp, et("2026-09-28T16:50:00"), engine_api=fake,
                notify=False, sync_playbook=False)
            self.assertEqual([call[0] for call in fake.calls], ["stop_tick", "checkin_pm"])
            self.assertTrue(result["marker"]["runs"]["checkin_pm"]["catchup"])

    def test_nfp_event_guard_blocks_entries_inside_fifteen_minutes(self):
        with tempfile.TemporaryDirectory() as temp:
            state = make_fx_state(temp)
            now = et("2026-10-02T08:20:00")  # NFP is scheduled at 08:30 ET
            result = engine.swing_open_run(
                state, now=now, price={"price": 4200, "symbol": "XAUUSD=X"},
                summary=fake_summary(now), notify=False, sync_playbook=False)
            self.assertEqual(result["status"], "event_guard")
            self.assertIn("NFP", result["skipped"][0])


class XAUUSDLifecycleTests(unittest.TestCase):
    def test_stop_is_checked_outside_windows_and_grades_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            state = make_fx_state(temp)
            now = et("2026-09-28T08:30:00")
            opened = engine.swing_open_run(
                state, now=now, price={"price": 4200.0, "symbol": "XAUUSD=X"},
                summary=fake_summary(now), notify=False, sync_playbook=False)
            self.assertEqual(opened["status"], "ok")
            trade = opened["opened"][0]
            self.assertGreater(trade["qty_oz"], 0)
            self.assertLess(trade["stop_price"], trade["entry_price"])
            self.assertAlmostEqual(trade["risk_usd"], 1000.0, delta=1.0)

            # 22:00 ET is outside every message window; dispatch still calls
            # fx_tick and the breached stop exits at the simulated market.
            later = et("2026-09-28T22:00:00")
            result = dispatch_fx.dispatch_tick(
                state, later, price=trade["stop_price"] - 1.0,
                notify=False, sync_playbook=False)
            self.assertEqual(result["actions"], [])
            self.assertEqual(len(result["tick"].get("closed") or []), 1)
            self.assertEqual(result["tick"]["closed"][0]["status"], "stopped")
            stored = json.loads((state / "state.json").read_text())
            closed = stored["ledger"][0]
            self.assertEqual(closed["exit_reason"], "stopped")
            self.assertIn("hypothesis_correct", closed)
            self.assertTrue(any(entry.get("realized") for entry in stored["score_history"]))

    def test_swing_is_not_daily_flattened_but_friday_risk_check_flattens(self):
        with tempfile.TemporaryDirectory() as temp:
            state = make_fx_state(temp)
            open_time = et("2026-09-28T08:30:00")
            opened = engine.swing_open_run(
                state, now=open_time, price={"price": 4200.0, "symbol": "XAUUSD=X"},
                summary=fake_summary(open_time), notify=False, sync_playbook=False)
            self.assertEqual(opened["status"], "ok")

            # Tuesday risk check manages/trails but must not close the position.
            tuesday = et("2026-09-29T14:15:00")
            managed = engine.risk_check_run(
                state, now=tuesday, price={"price": 4210.0, "symbol": "XAUUSD=X"},
                summary=fake_summary(tuesday), block="checkin_pm",
                notify=False, sync_playbook=False)
            self.assertEqual(managed["closed"], [])
            stored = json.loads((state / "state.json").read_text())
            self.assertEqual(stored["ledger"][0]["status"], "open")

            friday = et("2026-10-02T14:15:00")
            weekend = engine.risk_check_run(
                state, now=friday, price={"price": 4212.0, "symbol": "XAUUSD=X"},
                summary=fake_summary(friday), block="checkin_pm",
                notify=False, sync_playbook=False)
            self.assertEqual(len(weekend["closed"]), 1)
            self.assertEqual(weekend["closed"][0]["status"], "weekend_flat")


if __name__ == "__main__":
    unittest.main()
