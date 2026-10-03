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
  Mon–Fri 12:00–14:30  open catch-up (only if the day's open slot is
                       still unset — the whole morning was missed)
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

No double-open, even if the marker is lost: the once-per-day markers only
survive between Actions runs while the workflow's "Persist state" step can
push state/last_dispatch.json. If that push conflicts or fails, the next
dispatch would re-enter the open window with a fresh marker. As a second
line of defence, the open window also checks the state ledger: if a
position opened on the current ET day is still open, the day's open has
already happened, so open_run is skipped (and the `open` marker is healed
so later dispatches take the cheap path). FORCE_DISPATCH=1 bypasses both
guards.

Empty research never burns the day's open slot: an open_run whose research
fetch came back with nothing (no notes, no prices) reports
`research.ok == False`. That is a measurement failure, not a flat market, so
the slot stays open for up to MAX_OPEN_EMPTY_ATTEMPTS retries (the
"Opened today: none" alert is sent at most once a day via its own
`open_none` marker). A run that *did* see research but found no hypothesis
at or above min_confidence is a legitimately flat day: run once, notify
once, then mark the slot as done.

Open catch-up: if the day's `open` marker is still unset by 12:00 ET (the
whole morning was missed — GitHub cron was late again), open_run may also
run until 14:30 ET. Late entries are safe: the 15:50 close plus the next
morning's close_catchup flatten everything opened that day.
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
WINDOW_OPEN_CATCHUP = (720, 870)   # Mon–Fri 12:00–14:30 (morning open missed)
WINDOW_CHECKIN = (630, 780)        # Mon–Fri 10:30–13:00
WINDOW_CLOSE = (950, 1110)         # Mon–Fri 15:50–18:30
WINDOW_PREVIEW = (1200, 1350)      # Mon–Fri 20:00–22:30

# How many times a day an open_run that saw *no research at all* may retry
# before the day is given up on (a legitimately flat day never retries).
MAX_OPEN_EMPTY_ATTEMPTS = 3


def _in_window(hm: int, window: tuple) -> bool:
    return window[0] <= hm <= window[1]


def _today() -> str:
    """Current ET trading day (the runner clock is UTC)."""
    return datetime.now(ET).date().isoformat()


def _marker() -> dict:
    try:
        with open(MARKER) as f:
            return json.load(f)
    except Exception:
        return {}


def _write_marker(m: dict) -> None:
    os.makedirs(os.path.dirname(MARKER), exist_ok=True)
    with open(MARKER, "w") as f:
        json.dump(m, f)


def _already_ran(run_type: str) -> bool:
    if _force_dispatch():
        return False
    m = _marker()
    return m.get("date") == _today() and bool(m.get(run_type))


def _mark_ran(run_type: str, **extra) -> None:
    m = _marker()
    if m.get("date") != _today():
        m = {}  # new ET day: drop yesterday's once-per-day flags
    m["date"] = _today()
    m[run_type] = True
    m.update(extra)
    _write_marker(m)


def _open_attempts() -> int:
    """Empty-research open attempts already spent on today's ET date."""
    m = _marker()
    if m.get("date") != _today():
        return 0
    try:
        return int(m.get("open_attempts") or 0)
    except (TypeError, ValueError):
        return 0


def _note_open_attempt() -> int:
    """Count one more empty-research open attempt; returns the new total."""
    n = _open_attempts() + 1
    m = _marker()
    if m.get("date") != _today():
        m = {}
    m["date"] = _today()
    m["open_attempts"] = n
    _write_marker(m)
    return n


def _open_outcome(res) -> str:
    """Classify an open_run result: 'traded' | 'flat' | 'empty' | 'no_run'.

    - traded : at least one position opened → the day's slot is spent
    - flat   : research was fine, nothing cleared min_confidence → spent
    - empty  : research fetch returned nothing → retry, do NOT spend the slot
    - no_run : paused / skipped / no result → leave the slot alone entirely

    A result without a `research` report (older engine, simulation stub) is
    treated as `flat`, i.e. the historical behaviour.
    """
    if not isinstance(res, dict) or res.get("status") != "ok":
        return "no_run"
    if res.get("opened"):
        return "traded"
    research = res.get("research")
    if isinstance(research, dict) and not research.get("ok", True):
        return "empty"
    return "flat"


def _open_ledger_positions() -> list:
    """Trades still marked open in the state ledger (state/state.json)."""
    try:
        with open(os.path.join(STATE_DIR, "state.json")) as f:
            data = json.load(f)
    except Exception:
        return []
    return [t for t in (data.get("ledger") or []) if t.get("status") == "open"]


def _trade_day(trade: dict):
    """ET calendar day the trade was opened on, or None if unparseable."""
    raw = trade.get("opened_at") or ""
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(ET).date()


def _opened_on_prior_day(trade: dict, today) -> bool:
    day = _trade_day(trade)
    return day is not None and day < today  # unknown age — never treat as stale


def _force_dispatch() -> bool:
    return os.environ.get("FORCE_DISPATCH") == "1"


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

    # --- open window: 09:25–12:00 ET (+ 12:00–14:30 catch-up when the
    #     morning was missed entirely and the slot is still unset)
    if _in_window(hm, WINDOW_OPEN) or _in_window(hm, WINDOW_OPEN_CATCHUP):
        handled = True
        if _already_ran("open"):
            print("[dispatch] open run already done today — skip")
        else:
            # Belt and braces: even with the marker missing (state persist
            # failed), an open ledger position opened today proves the open
            # already ran — opening again would double the book. FORCE_DISPATCH
            # deliberately bypasses both once-per-day guards.
            opened_today = [t for t in open_positions if _trade_day(t) == today]
            if opened_today and not _force_dispatch():
                syms = ", ".join(t.get("symbol", "?") for t in opened_today)
                print(f"[dispatch] positions already open today ({syms}) — "
                      "open already done today — skip (marker healed)")
                _mark_ran("open")
            elif not _in_window(hm, WINDOW_OPEN) and _open_attempts() >= MAX_OPEN_EMPTY_ATTEMPTS:
                print(f"[dispatch] open catch-up window but {_open_attempts()} empty-research "
                      "attempts already spent today — skip")
            else:
                if _in_window(hm, WINDOW_OPEN):
                    print("[dispatch] opening window → running open_run")
                else:
                    print("[dispatch] open catch-up window (morning open missed) "
                          "→ running open_run")
                # Only let the engine send the "Opened today: none" alert on
                # the first conclusive run of the day, never on a retry tick.
                allow_notify = not _already_ran("open_none")
                res = engine.open_run(STATE_DIR, notify_none=allow_notify)
                outcome = _open_outcome(res)
                opened_now = bool(isinstance(res, dict) and res.get("opened"))
                if allow_notify and not opened_now:
                    # The flat-day notice is now out for today; any retry only
                    # re-runs the research fetch and stays quiet.
                    _mark_ran("open_none")
                if outcome == "empty":
                    n = _note_open_attempt()
                    if n >= MAX_OPEN_EMPTY_ATTEMPTS:
                        print(f"[dispatch] research still empty after {n} attempts — "
                              "giving up on today's open (slot closed)")
                        _mark_ran("open")
                    else:
                        print(f"[dispatch] research came back empty (attempt "
                              f"{n}/{MAX_OPEN_EMPTY_ATTEMPTS}) — open slot kept "
                              "for a retry")
                elif outcome == "no_run":
                    print("[dispatch] open run did not execute "
                          f"({(res or {}).get('status') or 'no result'}) — "
                          "open slot kept for a retry")
                else:
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
