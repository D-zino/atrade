#!/usr/bin/env python3
"""Scheduler dispatcher for cloud schedulers (e.g. GitHub Actions).

Run frequently (e.g. every 10 minutes on weekdays). It checks the real
America/New_York time and executes the open run (09:25 ET) or close run
(15:50 ET) only when inside the correct window. Handles DST automatically,
holidays, and the pause flag via the engine's own guards.

A marker file (state/last_dispatch.json) makes it idempotent: each run type
executes at most once per trading day, even if the scheduler fires twice in
the same window. The daily flags reset automatically when the ET date rolls
over, so yesterday's flags can never suppress today's runs.

Env switches (used by the GitHub Actions workflow inputs):
  SEND_TEST_TELEGRAM=1   send a test Telegram message and exit
  SEND_CHECKIN=1         send a mid-session check-in now (test)
  SEND_PREVIEW=1         send a tomorrow-preview now (test)
  SEND_WEEK_AHEAD=1      send a week-ahead digest now (test)
  RESUME=1               resume the agent after an auto-pause
  FORCE_DISPATCH=1       bypass the once-per-day marker (debugging)

Windows (ET) — deliberately wide: cloud cron is often delayed by hours, so
any dispatch that lands inside a window still executes the run:
  Sun 17:00–19:00  week-ahead digest (Sunday only)
  Mon–Fri 09:25–12:00  open run
  Mon–Fri 10:30–13:00  mid-session check-in
  Mon–Fri 15:50–18:30  close run + self-improvement loop
  Mon–Fri 20:00–22:30  tomorrow preview

Catch-up close: if the ledger still shows open positions after the close
window was missed, close_run runs anyway (close_day_trades flattens every
open ledger position, no matter when it was opened):
  - after 15:50 ET on the same trading day — fills that day's `close` slot
    (also covers dispatches landing after 18:30 with positions still open)
  - on a later trading day before 15:50 — stale overnight positions are
    flattened before the open window adds new ones; gated by its own
    once-per-day `close_catchup` marker so the regular 15:50 close still
    runs for the new session.
"""
import json
import os
import sys
import traceback
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ET = ZoneInfo("America/New_York")
STATE_DIR = os.path.join(ROOT, "state")
MARKER = os.path.join(ROOT, "state", "last_dispatch.json")

# Dispatch windows as (start, end) minutes-from-midnight ET, inclusive.
WINDOW_WEEK_AHEAD = (1020, 1140)   # Sun 17:00–19:00
WINDOW_OPEN = (565, 720)           # Mon–Fri 09:25–12:00
WINDOW_CHECKIN = (630, 780)        # Mon–Fri 10:30–13:00
WINDOW_CLOSE = (950, 1110)         # Mon–Fri 15:50–18:30
WINDOW_PREVIEW = (1200, 1350)      # Mon–Fri 20:00–22:30


def _in_window(hm: int, window: tuple) -> bool:
    return window[0] <= hm <= window[1]


def _marker() -> dict:
    try:
        with open(MARKER) as f:
            return json.load(f)
    except Exception:
        return {}


def _already_ran(run_type: str) -> bool:
    if os.environ.get("FORCE_DISPATCH") == "1":
        return False
    m = _marker()
    today = datetime.now(ET).date().isoformat()
    return m.get("date") == today and bool(m.get(run_type))


def _mark_ran(run_type: str) -> None:
    today = datetime.now(ET).date().isoformat()
    m = _marker()
    if m.get("date") != today:
        m = {}  # new ET day: drop yesterday's once-per-day flags
    m["date"] = today
    m[run_type] = True
    os.makedirs(os.path.dirname(MARKER), exist_ok=True)
    with open(MARKER, "w") as f:
        json.dump(m, f)


def _open_ledger_positions() -> list:
    """Trades still marked open in the state ledger (state/state.json)."""
    try:
        with open(os.path.join(STATE_DIR, "state.json")) as f:
            data = json.load(f)
    except Exception:
        return []
    return [t for t in (data.get("ledger") or []) if t.get("status") == "open"]


def _opened_on_prior_day(trade: dict, today) -> bool:
    """True when the trade was opened on an earlier ET calendar day."""
    raw = trade.get("opened_at") or ""
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return False  # unknown age — never treat as stale
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET).date() < today


def main() -> int:
    from atrade import market, util

    util.configure_logging(os.path.join(ROOT, "state", "logs"))
    now = datetime.now(ET)
    print(f"[dispatch] {now.isoformat(timespec='minutes')} ET | market={market.market_status(now)}")

    # .env keys (if present locally) are made available to the process
    from atrade import config as config_mod
    keys = config_mod.load_env_keys()
    for k, v in keys.items():
        os.environ.setdefault(k, v)

    # Import telegram first so a test ping still works if the engine stack
    # has a problem (the original check-in test never got that far).
    from atrade import telegram

    if not telegram.configured():
        print("[dispatch] Telegram not configured — alerts disabled. "
              "Set repo secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
              "(Settings → Secrets and variables → Actions).")

    def _require_telegram() -> int | None:
        """Fail Action *test* steps loudly when secrets are missing."""
        if telegram.configured():
            return None
        print("[dispatch] ERROR: Telegram is not configured.")
        print("Add these as GitHub repo secrets (Settings → Secrets and variables → Actions):")
        print("  TELEGRAM_BOT_TOKEN   from @BotFather")
        print("  TELEGRAM_CHAT_ID     from https://api.telegram.org/bot<TOKEN>/getUpdates")
        print("Message the bot once first, otherwise Telegram will not deliver.")
        print("Group chats have a negative chat id (e.g. -100123...).")
        return 1

    def _require_sent(label: str) -> int:
        if telegram.last_ok:
            print(f"[dispatch] {label} telegram sent")
            return 0
        err = telegram.last_error or "send returned False"
        print(f"[dispatch] ERROR: {label} telegram was not delivered: {err}")
        print("Check TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID. You must message the bot once.")
        return 1

    # --- optional: send a Telegram test message -----------------------------
    if os.environ.get("SEND_TEST_TELEGRAM") == "1":
        missing = _require_telegram()
        if missing:
            return missing
        ok = telegram.send(telegram.format_test())
        return 0 if ok else _require_sent("test")

    from atrade import engine

    # --- optional: send check-in / preview / week-ahead now (for testing) ---
    if os.environ.get("SEND_CHECKIN") == "1":
        missing = _require_telegram()
        if missing:
            return missing
        r = engine.checkin_run(STATE_DIR, force_mock=False, allow_anyday=True)
        print(f"[dispatch] checkin -> {r.get('status')}")
        if r.get("status") == "paused":
            print("[dispatch] ERROR: agent is paused — no check-in sent. Resume first.")
            return 1
        return _require_sent("check-in")
    if os.environ.get("SEND_PREVIEW") == "1":
        missing = _require_telegram()
        if missing:
            return missing
        r = engine.preview_run(STATE_DIR, force_mock=False, allow_anyday=True)
        print(f"[dispatch] preview -> {r.get('status')}")
        if r.get("status") == "paused":
            print("[dispatch] ERROR: agent is paused — no preview sent. Resume first.")
            return 1
        return _require_sent("preview")
    if os.environ.get("SEND_WEEK_AHEAD") == "1":
        missing = _require_telegram()
        if missing:
            return missing
        r = engine.week_ahead_run(STATE_DIR, force_mock=False, allow_anyday=True)
        print(f"[dispatch] week-ahead -> {r.get('status')}")
        return _require_sent("week-ahead")

    # --- optional: resume after auto-pause ----------------------------------
    if os.environ.get("RESUME") == "1":
        r = engine.resume(STATE_DIR)
        print(f"[dispatch] resume -> {r}")
        return 0

    hm = now.hour * 60 + now.minute
    today = now.date()
    handled = False

    # --- weekly digest: Sundays 17:00–19:00 ET (weekend — before market guard)
    if now.weekday() == 6 and _in_window(hm, WINDOW_WEEK_AHEAD):
        if _already_ran("week_ahead"):
            print("[dispatch] week-ahead already sent this week — skip")
        else:
            print("[dispatch] Sunday week-ahead window → running week_ahead_run")
            engine.week_ahead_run(STATE_DIR)
            _mark_ran("week_ahead")
        return 0

    if not market.is_trading_day(today):
        print("[dispatch] not a trading day — nothing to do")
        return 0

    open_positions = _open_ledger_positions()
    stale = [t for t in open_positions if _opened_on_prior_day(t, today)]

    # --- catch-up close: stale positions from an earlier session -------------
    # A later trading day still owes those positions a close run (e.g. the
    # whole close window was missed). Runs before the open window so leftovers
    # are flattened before new ones are opened, and it does NOT consume the
    # day's `close` slot — the regular 15:50 close still runs for today's book.
    if hm < WINDOW_CLOSE[0] and stale and not _already_ran("close_catchup"):
        handled = True
        syms = ", ".join(t.get("symbol", "?") for t in stale)
        print(f"[dispatch] catch-up close (stale open positions: {syms}) → running close_run")
        engine.close_run(STATE_DIR)
        _mark_ran("close_catchup")

    # --- open window: 09:25–12:00 ET
    if _in_window(hm, WINDOW_OPEN):
        handled = True
        if _already_ran("open"):
            print("[dispatch] open run already done today — skip")
        else:
            print("[dispatch] opening window → running open_run")
            engine.open_run(STATE_DIR)
            _mark_ran("open")

    # --- check-in window: 10:30–13:00 ET
    if _in_window(hm, WINDOW_CHECKIN):
        handled = True
        if _already_ran("checkin"):
            print("[dispatch] check-in already done today — skip")
        else:
            print("[dispatch] check-in window → running checkin_run")
            engine.checkin_run(STATE_DIR)
            _mark_ran("checkin")

    # --- close window: 15:50–18:30 ET; catch-up any later dispatch ----------
    # Inside the window close_run always runs (evaluation + learning loop).
    # After the window (cron late again) it still runs when the ledger has
    # open positions, even past 18:30 — that catch-up is the day's `close`.
    if hm >= WINDOW_CLOSE[0] and (_in_window(hm, WINDOW_CLOSE) or open_positions):
        handled = True
        if _already_ran("close"):
            print("[dispatch] close run already done today — skip")
        else:
            if _in_window(hm, WINDOW_CLOSE):
                print("[dispatch] closing window → running close_run")
            else:
                syms = ", ".join(t.get("symbol", "?") for t in open_positions)
                print(f"[dispatch] catch-up close (open positions after 15:50 ET: {syms}) → running close_run")
            engine.close_run(STATE_DIR)
            _mark_ran("close")

    # --- tomorrow preview window: 20:00–22:30 ET
    if _in_window(hm, WINDOW_PREVIEW):
        handled = True
        if _already_ran("preview"):
            print("[dispatch] preview already done today — skip")
        else:
            print("[dispatch] preview window → running preview_run")
            engine.preview_run(STATE_DIR)
            _mark_ran("preview")

    if not handled:
        print("[dispatch] outside run windows — no-op")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
