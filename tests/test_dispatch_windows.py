"""Regression tests for the stock dispatcher's windows and catch-up rules.

These pin the guarantees for US sessions:
  - open 09:25–12:00 ET, check-in 10:30–13:00, close 15:50–18:30,
    preview 20:00–22:30 (GitHub cron is often late; windows are wide)
  - every run at most once per trading day via state/last_dispatch.json
  - a still-open ledger after 15:50 ET forces a close run even outside
    the close window, and stale overnight positions are flattened on the
    next trading morning before the open window
  - no double-open even when the marker file is lost (state persist push
    failed): a position already opened today in the ledger suppresses
    open_run; FORCE_DISPATCH=1 bypasses both guards

The engine is stubbed (call recorder); time is frozen by replacing the
module's `datetime`. dispatch.py's ROOT/STATE_DIR/MARKER are pointed at a
temp dir, so the real state/ is never touched. FX is not covered here
(see tests/test_fx_calendar.py, tests/test_fx_paper_book.py).
"""
from __future__ import annotations

import importlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")

MON = "2026-09-28"  # trading days Tue 09-29, Wed 09-30 follow
TUE = "2026-09-29"
WED = "2026-09-30"
SAT = "2026-10-03"
SUN = "2026-10-04"
LABOR_DAY = "2026-09-07"  # NYSE holiday (Mon)


class Frozen(datetime):
    """datetime with a controllable now()."""
    when: datetime = None  # set per run

    @classmethod
    def now(cls, tz=None):
        return cls.when.astimezone(tz) if tz else cls.when


class _EngineStub:
    def __init__(self):
        self.calls: list[str] = []
        self.raise_close = False

    def _rec(self, name):
        def f(*a, **k):
            self.calls.append(name)
            if name == "close_run" and self.raise_close:
                raise RuntimeError("alpaca down")
            return {"status": "ok"}
        return f

    def __getattr__(self, name):
        if name.startswith("raise_") or name == "calls":
            raise AttributeError(name)
        return self._rec(name)


def pos(symbol, opened_at, status="open"):
    return {"symbol": symbol, "status": status, "opened_at": opened_at,
            "id": f"{symbol}:{opened_at}"}


_unset = object()


class DispatcherCase(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            "dispatch_under_test", REPO / "deploy" / "dispatch.py")
        self.d = importlib.util.module_from_spec(spec)
        sys.modules["dispatch_under_test"] = self.d
        spec.loader.exec_module(self.d)

        self.tmp = Path(tempfile.mkdtemp(prefix="atrade-disp-test-"))
        (self.tmp / "state").mkdir()
        self.d.ROOT = str(self.tmp)
        self.d.STATE_DIR = str(self.tmp / "state")
        self.d.MARKER = str(self.tmp / "state" / "last_dispatch.json")
        self.d.datetime = Frozen

        import atrade
        self.engine = _EngineStub()
        self._real_engine = sys.modules.get("atrade.engine")
        self._real_engine_attr = getattr(atrade, "engine", None)
        sys.modules["atrade.engine"] = self.engine
        atrade.engine = self.engine
        from atrade import telegram
        self._real_configured = telegram.configured
        telegram.configured = lambda: False

    def tearDown(self):
        import atrade
        if self._real_engine is not None:
            sys.modules["atrade.engine"] = self._real_engine
        else:
            sys.modules.pop("atrade.engine", None)
        if self._real_engine_attr is not None:
            atrade.engine = self._real_engine_attr
        from atrade import telegram
        telegram.configured = self._real_configured
        sys.modules.pop("dispatch_under_test", None)

    # ------------------------------------------------------------------
    def run_dispatch(self, at, *, ledger=None, marker=_unset, env=None):
        self.engine.calls.clear()
        Frozen.when = datetime.fromisoformat(f"{at}").replace(tzinfo=ET)
        (self.tmp / "state" / "state.json").write_text(
            json.dumps({"ledger": ledger or []}))
        if marker is _unset:  # keep marker from previous call in same test
            pass
        elif marker is None:
            try:
                os.remove(self.d.MARKER)
            except OSError:
                pass
        else:
            Path(self.d.MARKER).write_text(json.dumps(marker))
        over = dict(env or {})
        over.setdefault("FORCE_DISPATCH", "")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, over, clear=True):
            if over["FORCE_DISPATCH"] == "":
                os.environ.pop("FORCE_DISPATCH", None)
            try:
                rc = self.d.main()
            except Exception as e:  # dispatcher must not swallow close errors
                rc = 99
                buf.write(f"EXC {e!r}")
        try:
            mk = json.loads(Path(self.d.MARKER).read_text())
        except Exception:
            mk = {}
        self.calls = self.engine.calls
        return rc, buf.getvalue(), mk



class WindowsTests(DispatcherCase):
    def test_spec_windows_exact(self):
        self.assertEqual(self.d.WINDOW_OPEN, (565, 720))      # 09:25–12:00
        self.assertEqual(self.d.WINDOW_CHECKIN, (630, 780))    # 10:30–13:00
        self.assertEqual(self.d.WINDOW_CLOSE, (950, 1110))    # 15:50–18:30
        self.assertEqual(self.d.WINDOW_PREVIEW, (1200, 1350))  # 20:00–22:30
        self.assertEqual(self.d.WINDOW_WEEK_AHEAD, (1020, 1140))  # Sun 17:00–19:00

    def test_open_window_boundaries(self):
        rc, out, mk = self.run_dispatch(f"{TUE}T09:20", marker=None)
        self.assertEqual(self.calls, [])                       # too early
        rc, out, mk = self.run_dispatch(f"{TUE}T09:25", marker=None)
        self.assertEqual(self.calls, ["open_run"])              # start incl.
        rc, out, mk = self.run_dispatch(f"{TUE}T09:26")
        self.assertEqual(self.calls, [])                        # once-per-day
        rc, out, mk = self.run_dispatch(f"{TUE}T12:01", marker=None)
        self.assertNotIn("open_run", self.calls)                # end passed

    def test_checkin_window_boundaries(self):
        rc, out, mk = self.run_dispatch(f"{TUE}T10:30", marker=None)
        self.assertEqual(self.calls, ["open_run", "checkin_run"])
        rc, out, mk = self.run_dispatch(f"{TUE}T13:00")
        self.assertEqual(self.calls, [])                        # boundary: both ran
        rc, out, mk = self.run_dispatch(f"{TUE}T13:01", marker={"date": TUE, "open": True})
        self.assertNotIn("checkin_run", self.calls)             # end passed

    def test_close_window_and_late_catchup(self):
        rc, out, mk = self.run_dispatch(f"{TUE}T15:50", marker=None)
        self.assertEqual(self.calls, ["close_run"])              # window start
        rc, out, mk = self.run_dispatch(f"{TUE}T18:30")
        self.assertNotIn("close_run", self.calls)                # already ran today
        # after the window: nothing to do with a flat ledger
        rc, out, mk = self.run_dispatch(f"{TUE}T18:31", marker={"date": TUE})
        self.assertEqual(self.calls, [])
        # but with the ledger still open, close runs even past 18:30
        rc, out, mk = self.run_dispatch(f"{TUE}T23:59", marker={"date": TUE},
                                        ledger=[pos("NVDA", f"{TUE}T13:30:00+00:00")])
        self.assertEqual(self.calls, ["close_run"])
        self.assertTrue(mk.get("close"))          # fills the day's close slot
        self.assertNotIn("close_catchup", mk)

    def test_preview_window(self):
        rc, out, mk = self.run_dispatch(f"{TUE}T20:00", marker=None)
        self.assertEqual(self.calls, ["preview_run"])
        rc, out, mk = self.run_dispatch(f"{TUE}T22:30")
        self.assertEqual(self.calls, [])                         # once-per-day
        rc, out, mk = self.run_dispatch(f"{TUE}T22:31", marker=None)
        self.assertEqual(self.calls, [])                         # too late

    def test_close_failure_does_not_burn_the_slot(self):
        self.engine.raise_close = True
        rc, out, mk = self.run_dispatch(f"{TUE}T15:50", marker=None,
                                        ledger=[pos("NVDA", f"{TUE}T13:30:00+00:00")])
        self.assertEqual(rc, 99)
        self.assertNotIn("close", mk)                  # slot NOT consumed
        self.engine.raise_close = False
        rc, out, mk = self.run_dispatch(f"{TUE}T16:00")
        self.assertEqual(self.calls, ["close_run"])    # next tick retries


class StaleAndGuardTests(DispatcherCase):
    def test_stale_positions_caught_up_next_morning(self):
        stale = [pos("NVDA", "2026-09-02T13:42:43+00:00")]   # "sat 2–28 Sep"
        rc, out, mk = self.run_dispatch(f"{MON}T09:30", marker=None, ledger=stale)
        self.assertEqual(self.calls, ["close_run", "open_run"])  # flatten, then open
        self.assertTrue(mk.get("close_catchup"))
        self.assertNotIn("close", mk)        # regular 15:50 close still owed
        rc, out, mk = self.run_dispatch(f"{MON}T09:40", ledger=stale)
        self.assertEqual(self.calls, [])                        # no double anything
        rc, out, mk = self.run_dispatch(f"{MON}T15:50", ledger=stale)
        self.assertEqual(self.calls, ["close_run"])              # today's close runs

    def test_no_double_open_when_marker_lost(self):
        today_pos = [pos("NVDA", f"{TUE}T13:30:00+00:00")]  # opened 09:30 ET
        rc, out, mk = self.run_dispatch(f"{TUE}T10:00", marker=None, ledger=today_pos)
        self.assertEqual(self.calls, [])            # marker lost -> ledger knows
        self.assertTrue(mk.get("open"))             # marker healed for the day
        # FORCE_DEBUG bypass for manual re-runs
        rc, out, mk = self.run_dispatch(f"{TUE}T10:10", marker=None, ledger=today_pos,
                                        env={"FORCE_DISPATCH": "1"})
        self.assertEqual(self.calls, ["open_run"])

    def test_stale_only_ledger_does_not_block_todays_open(self):
        stale = [pos("NVDA", "2026-09-02T13:42:43+00:00")]
        rc, out, mk = self.run_dispatch(f"{MON}T10:00", marker=None, ledger=stale)
        self.assertEqual(self.calls, ["close_run", "open_run"])
        # marker-only day (close_catchup ran, open did not): today's position
        # in the ledger means the open already happened
        mixed = stale + [pos("AAPL", f"{MON}T13:40:00+00:00")]
        rc, out, mk = self.run_dispatch(
            f"{MON}T10:05", marker={"date": MON, "close_catchup": True}, ledger=mixed)
        self.assertEqual(self.calls, [])

    def test_unparseable_opened_at_never_suppresses_open(self):
        garbage = [pos("XLE", "not-a-timestamp")]
        rc, out, mk = self.run_dispatch(f"{TUE}T09:30", marker=None, ledger=garbage)
        self.assertEqual(self.calls, ["open_run"])   # unknown age is not "today"
        rc, out, mk = self.run_dispatch(f"{TUE}T16:00", ledger=garbage)
        self.assertEqual(self.calls, ["close_run"])  # still closed same day

    def test_non_trading_days_quiet(self):
        stale = [pos("NVDA", "2026-09-02T13:42:43+00:00")]
        rc, out, mk = self.run_dispatch(f"{SAT}T09:30", marker=None, ledger=stale)
        self.assertEqual(self.calls, [])                     # Saturday
        rc, out, mk = self.run_dispatch(f"{LABOR_DAY}T09:30", marker=None, ledger=stale)
        self.assertEqual(self.calls, [])                     # holiday
        rc, out, mk = self.run_dispatch(f"{SUN}T17:30", marker=None)
        self.assertEqual(self.calls, ["week_ahead_run"])     # Sunday digest
        rc, out, mk = self.run_dispatch(f"{SUN}T18:00")
        self.assertEqual(self.calls, [])                     # once per week


if __name__ == "__main__":
    unittest.main()
