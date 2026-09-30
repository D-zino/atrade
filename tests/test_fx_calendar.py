from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from atrade import market_fx
from deploy import dispatch_fx

ET = market_fx.ET


def et(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ET)


class FXCalendarTests(unittest.TestCase):
    def test_fx_day_rollover_and_dst(self):
        self.assertEqual(market_fx.fx_day(et("2026-10-04T16:59:00" )).isoformat(), "2026-10-04")
        self.assertEqual(market_fx.fx_day(et("2026-10-04T18:00:00")).isoformat(), "2026-10-05")
        self.assertEqual(market_fx.fx_day(et("2026-10-05T16:59:00")).isoformat(), "2026-10-05")
        self.assertEqual(market_fx.fx_day(et("2026-10-05T17:00:00")).isoformat(), "2026-10-06")
        # Nov 1 is the US fall-back date; FX labels still use local ET wall time.
        fall_back = datetime(2026, 11, 1, 18, 0, tzinfo=ET)
        self.assertEqual(fall_back.utcoffset().total_seconds(), -5 * 3600)
        self.assertEqual(market_fx.fx_day(fall_back).isoformat(), "2026-11-02")

    def test_weekend_break_and_named_sessions(self):
        self.assertEqual(market_fx.fx_session(et("2026-10-03T12:00:00")), "weekend")
        self.assertEqual(market_fx.fx_session(et("2026-10-04T16:59:00")), "weekend")
        self.assertEqual(market_fx.fx_session(et("2026-10-04T17:30:00")), "break")
        self.assertEqual(market_fx.fx_session(et("2026-10-04T18:00:00")), "asia")
        self.assertEqual(market_fx.fx_session(et("2026-10-05T08:30:00")), "overlap")
        self.assertIn("london", market_fx.fx_sessions(et("2026-10-05T08:30:00")))
        self.assertIn("ny", market_fx.fx_sessions(et("2026-10-05T08:30:00")))
        self.assertEqual(market_fx.fx_session(et("2026-10-05T17:30:00")), "break")
        self.assertFalse(market_fx.is_fx_open(et("2026-10-05T17:30:00")))

    def test_thin_holiday_calendar(self):
        self.assertFalse(market_fx.is_fx_trading_day(datetime(2027, 1, 1, 12, tzinfo=ET)))
        self.assertEqual(market_fx.fx_session(datetime(2027, 1, 1, 12, tzinfo=ET)), "holiday")
        self.assertFalse(market_fx.is_fx_trading_day(datetime(2026, 12, 25, 10, tzinfo=ET)))

    def test_all_dispatch_windows_have_inclusive_edges(self):
        dates = {
            "open": "2026-10-05",
            "checkin_am": "2026-10-05",
            "checkin_pm": "2026-10-05",
            "preview": "2026-10-05",
            "week_ahead": "2026-10-04",
        }
        for name, (start, end) in dispatch_fx.WINDOWS.items():
            day = dates[name]
            at_start = datetime.fromisoformat(f"{day}T{start // 60:02d}:{start % 60:02d}:00").replace(tzinfo=ET)
            at_end = datetime.fromisoformat(f"{day}T{end // 60:02d}:{end % 60:02d}:00").replace(tzinfo=ET)
            before = datetime.fromtimestamp(at_start.timestamp() - 60, ET)
            after = datetime.fromtimestamp(at_end.timestamp() + 60, ET)
            self.assertTrue(dispatch_fx.window_contains(name, at_start), name)
            self.assertTrue(dispatch_fx.window_contains(name, at_end), name)
            self.assertFalse(dispatch_fx.window_contains(name, before), name)
            self.assertFalse(dispatch_fx.window_contains(name, after), name)

    def test_marker_rolls_on_fx_day_not_midnight(self):
        class StubEngine:
            def __init__(self):
                self.calls = []
                self.entries = {"2026-10-05"}

            def fx_tick(self, state_dir, now=None, price=None, **kwargs):
                self.calls.append(("tick", now))
                return {"status": "ok", "price": 4200.0, "source": "fake"}

            def risk_check_run(self, state_dir, now=None, block=None, **kwargs):
                self.calls.append((block, now))
                return {"status": "ok"}

            def fx_open_positions(self, state_dir):
                return []

            def fx_had_entry(self, state_dir, fx_day):
                return fx_day in self.entries

        with tempfile.TemporaryDirectory() as temp:
            engine = StubEngine()
            monday_pm = et("2026-10-05T16:45:00")
            first = dispatch_fx.dispatch_tick(temp, monday_pm, engine_api=engine,
                                              notify=False, sync_playbook=False)
            self.assertEqual(first["fx_day"], "2026-10-05")
            self.assertIn("checkin_pm", first["marker"]["runs"])
            self.assertEqual([call[0] for call in engine.calls], ["tick", "checkin_pm"])

            rollover_break = et("2026-10-05T17:00:00")
            second = dispatch_fx.dispatch_tick(temp, rollover_break, engine_api=engine,
                                               notify=False, sync_playbook=False)
            self.assertEqual(second["fx_day"], "2026-10-06")
            self.assertEqual(second["session"], "break")
            self.assertEqual(second["marker"]["runs"], {})
            self.assertEqual([call[0] for call in engine.calls][-1], "tick")


if __name__ == "__main__":
    unittest.main()
