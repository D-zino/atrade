"""Engine date handling is ET, not UTC (the runner clock is UTC).

A preview dispatched 20:00–22:30 ET runs when the UTC calendar has already
rolled over to the next day, so `date.today()` used to make the "next trading
day" label skip a session: the Thursday 2026-10-01 21:52 ET preview printed
"Next trading day: 2026-10-05" (Monday) instead of Friday 2026-10-02 — and
the watchlist it carried never got opened on the Friday.

Same class of bug for the report filenames (open_/close_YYYY-MM-DD.md) and
for the daily P&L bucket, which must group by ET trading day.

Network-free: research is replayed from a cached research/latest.json in a
temp state dir and Telegram is stubbed.
"""
from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
UTC = timezone.utc


class Frozen(datetime):
    """datetime with a controllable now()."""

    when: datetime = None

    @classmethod
    def now(cls, tz=None):
        return cls.when.astimezone(tz) if tz else cls.when

    @classmethod
    def utcnow(cls):
        return cls.when.astimezone(UTC).replace(tzinfo=None)


def _cached_research():
    """Minimal research summary: one bullish NVDA setup + 30 bars of history."""
    bars = [{"c": 200.0 + i * 0.5} for i in range(30)]
    return {
        "asof": "2026-10-01T20:55:00+00:00",
        "sources": ["cache"],
        "notes": [
            {"category": "technical", "tickers": ["NVDA"], "title": "NVDA technical bullish",
             "summary": "NVDA technical bullish; NVDA daily move +3.1%",
             "direction": "bullish", "strength": 0.7, "source": "bars",
             "date": "2026-10-01"},
        ],
        "prices": {"NVDA": {"close": 214.5}},
        "bars": {"NVDA": bars},
        "dynamic": {"scan_universe": ["NVDA"]},
    }


class EngineDateCase(unittest.TestCase):
    def setUp(self):
        from atrade import engine, telegram

        self.engine = engine
        self.tmp = Path(tempfile.mkdtemp(prefix="atrade-engine-et-"))
        (self.tmp / "state" / "research").mkdir(parents=True)
        (self.tmp / "state" / "research" / "latest.json").write_text(
            json.dumps(_cached_research()))

        self._env = mock.patch.dict(
            os.environ, {"ATRADE_STATE_DIR": str(self.tmp / "state")}, clear=False)
        self._env.start()
        self._real_dt = engine.datetime
        engine.datetime = Frozen
        self.sent: list[str] = []
        self._real_send = telegram.send
        telegram.send = lambda text, *a, **k: (self.sent.append(text) or True)

    def tearDown(self):
        from atrade import telegram
        self.engine.datetime = self._real_dt
        telegram.send = self._real_send
        self._env.stop()


class PreviewRunTests(EngineDateCase):
    def test_thursday_night_preview_labels_friday(self):
        """21:00 ET Thu == 01:00 UTC Fri on the runner: preview must say Fri."""
        Frozen.when = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)
        self.assertEqual(Frozen.when.astimezone(ET).date().isoformat(), "2026-10-01")
        self.assertEqual(Frozen.when.date().isoformat(), "2026-10-02")  # UTC is Fri

        res = self.engine.preview_run(self.tmp / "state")

        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["next_day"], "2026-10-02")     # Friday
        self.assertNotEqual(res["next_day"], "2026-10-05")   # the old UTC bug
        self.assertTrue(res["hypotheses"] >= 1)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("<b>Next trading day:</b> 2026-10-02", self.sent[0])
        self.assertNotIn("2026-10-05", self.sent[0])

    def test_friday_night_preview_labels_monday(self):
        Frozen.when = datetime(2026, 10, 3, 1, 10, tzinfo=UTC)   # Fri 21:10 ET
        res = self.engine.preview_run(self.tmp / "state")
        self.assertEqual(res["next_day"], "2026-10-05")           # Monday

    def test_monday_morning_preview_labels_tuesday(self):
        Frozen.when = datetime(2026, 9, 28, 13, 30, tzinfo=UTC)   # Mon 09:30 ET
        res = self.engine.preview_run(self.tmp / "state", allow_anyday=True)
        self.assertEqual(res["next_day"], "2026-09-29")           # Tuesday


class TodayEtTests(EngineDateCase):
    def test_today_et_is_exchange_time(self):
        Frozen.when = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)     # Thu 21:00 ET
        self.assertEqual(self.engine._today_et().isoformat(), "2026-10-01")
        Frozen.when = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)    # Fri 08:00 ET
        self.assertEqual(self.engine._today_et().isoformat(), "2026-10-02")
        Frozen.when = datetime(2026, 10, 2, 4, 0, tzinfo=UTC)     # Fri 00:00 ET
        self.assertEqual(self.engine._today_et().isoformat(), "2026-10-02")


class DailyPnlTests(EngineDateCase):
    def test_realized_pnl_buckets_by_et_trading_day(self):
        st = types.SimpleNamespace(ledger=[
            # closed 15:00 ET Thu 10-01 -> Thursday's bucket
            {"pnl": 100.0, "closed_at": "2026-10-01T19:00:00+00:00"},
            # closed 20:30 ET Thu 10-01 (UTC already Fri) -> still Thursday
            {"pnl": 50.0, "closed_at": "2026-10-02T00:30:00+00:00"},
            # closed 20:30 ET Fri 10-02 -> Friday's bucket
            {"pnl": 25.0, "closed_at": "2026-10-03T00:30:00+00:00"},
            # no closed_at -> never counted
            {"pnl": 999.0, "closed_at": None},
            # unparseable -> never counted
            {"pnl": 999.0, "closed_at": "not-a-timestamp"},
        ])
        Frozen.when = datetime(2026, 10, 3, 1, 0, tzinfo=UTC)     # Fri 21:00 ET
        self.assertAlmostEqual(self.engine._daily_pnl(st, []), 25.0)
        Frozen.when = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)     # Thu 21:00 ET
        self.assertAlmostEqual(self.engine._daily_pnl(st, []), 150.0)

    def test_naive_and_z_timestamps_parse(self):
        st = types.SimpleNamespace(ledger=[
            {"pnl": 10.0, "closed_at": "2026-10-02T15:00:00"},      # naive -> UTC
            {"pnl": 20.0, "closed_at": "2026-10-02T19:30:00Z"},     # Z suffix
        ])
        Frozen.when = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)      # 16:00 ET Oct 2
        self.assertAlmostEqual(self.engine._daily_pnl(st, []), 30.0)

    def test_unrealized_is_added(self):
        st = types.SimpleNamespace(ledger=[])
        Frozen.when = datetime(2026, 10, 2, 20, 0, tzinfo=UTC)
        self.assertAlmostEqual(
            self.engine._daily_pnl(st, [{"unrealized_pl": -12.5}]), -12.5)


if __name__ == "__main__":
    unittest.main()
