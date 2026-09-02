#!/usr/bin/env python3
"""Scheduler dispatcher for cloud schedulers (e.g. GitHub Actions).

Run frequently (e.g. every 10 minutes on weekdays). It checks the real
America/New_York time and executes the open run (09:25 ET) or close run
(15:50 ET) only when inside the correct window. Handles DST automatically,
holidays, and the pause flag via the engine's own guards.

A marker file (state/last_dispatch.json) makes it idempotent: each run type
executes at most once per trading day, even if the scheduler fires twice in
the same window.

Env switches (used by the GitHub Actions workflow inputs):
  SEND_TEST_TELEGRAM=1   send a test Telegram message and exit
  SEND_CHECKIN=1         send a mid-session check-in now (test)
  SEND_PREVIEW=1         send a tomorrow-preview now (test)
  SEND_WEEK_AHEAD=1      send a week-ahead digest now (test)
  RESUME=1               resume the agent after an auto-pause
  FORCE_DISPATCH=1       bypass the once-per-day marker (debugging)

Windows (ET):
  Sun 17:00–17:20  week-ahead digest (Sunday only)
  Mon–Fri 09:25–09:45  open run
  Mon–Fri 10:30–10:50  mid-session check-in
  Mon–Fri 15:50–16:10  close run + self-improvement loop
  Mon–Fri 20:00–20:20  tomorrow preview
"""
import json
import os
import sys
import traceback
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ET = ZoneInfo("America/New_York")
MARKER = os.path.join(ROOT, "state", "last_dispatch.json")


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
    m = _marker()
    m["date"] = datetime.now(ET).date().isoformat()
    m[run_type] = True
    os.makedirs(os.path.dirname(MARKER), exist_ok=True)
    with open(MARKER, "w") as f:
        json.dump(m, f)


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
        r = engine.checkin_run(os.path.join(ROOT, "state"), force_mock=False, allow_anyday=True)
        print(f"[dispatch] checkin -> {r.get('status')}")
        if r.get("status") == "paused":
            print("[dispatch] ERROR: agent is paused — no check-in sent. Resume first.")
            return 1
        return _require_sent("check-in")
    if os.environ.get("SEND_PREVIEW") == "1":
        missing = _require_telegram()
        if missing:
            return missing
        r = engine.preview_run(os.path.join(ROOT, "state"), force_mock=False, allow_anyday=True)
        print(f"[dispatch] preview -> {r.get('status')}")
        if r.get("status") == "paused":
            print("[dispatch] ERROR: agent is paused — no preview sent. Resume first.")
            return 1
        return _require_sent("preview")
    if os.environ.get("SEND_WEEK_AHEAD") == "1":
        missing = _require_telegram()
        if missing:
            return missing
        r = engine.week_ahead_run(os.path.join(ROOT, "state"), force_mock=False, allow_anyday=True)
        print(f"[dispatch] week-ahead -> {r.get('status')}")
        return _require_sent("week-ahead")

    # --- optional: resume after auto-pause ----------------------------------
    if os.environ.get("RESUME") == "1":
        r = engine.resume(os.path.join(ROOT, "state"))
        print(f"[dispatch] resume -> {r}")
        return 0

    hm = now.hour * 60 + now.minute

    # --- weekly digest: Sundays 17:00–17:20 ET (weekend — before market guard)
    if now.weekday() == 6 and 1020 <= hm <= 1040:
        if _already_ran("week_ahead"):
            print("[dispatch] week-ahead already sent this week — skip")
            return 0
        print("[dispatch] Sunday week-ahead window → running week_ahead_run")
        engine.week_ahead_run(os.path.join(ROOT, "state"))
        _mark_ran("week_ahead")
        return 0

    if not market.is_trading_day(now.date()):
        print("[dispatch] not a trading day — nothing to do")
        return 0

    # open window: 09:25–09:45 ET
    if 565 <= hm <= 585:
        if _already_ran("open"):
            print("[dispatch] open run already done today — skip")
            return 0
        print("[dispatch] opening window → running open_run")
        engine.open_run(os.path.join(ROOT, "state"))
        _mark_ran("open")
        return 0
    # check-in window: 10:30–10:50 ET
    if 630 <= hm <= 650:
        if _already_ran("checkin"):
            print("[dispatch] check-in already done today — skip")
            return 0
        print("[dispatch] check-in window → running checkin_run")
        engine.checkin_run(os.path.join(ROOT, "state"))
        _mark_ran("checkin")
        return 0
    # close window: 15:50–16:10 ET
    if 950 <= hm <= 970:
        if _already_ran("close"):
            print("[dispatch] close run already done today — skip")
            return 0
        print("[dispatch] closing window → running close_run")
        engine.close_run(os.path.join(ROOT, "state"))
        _mark_ran("close")
        return 0
    # tomorrow preview window: 20:00–20:20 ET
    if 1200 <= hm <= 1220:
        if _already_ran("preview"):
            print("[dispatch] preview already done today — skip")
            return 0
        print("[dispatch] preview window → running preview_run")
        engine.preview_run(os.path.join(ROOT, "state"))
        _mark_ran("preview")
        return 0
    print("[dispatch] outside run windows — no-op")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
