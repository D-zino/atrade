#!/usr/bin/env python3
"""FX-day dispatcher for the isolated XAUUSD swing-with-stops paper book.

This module does not import/call deploy/dispatch.py and never touches the
NYSE equity schedule. It runs tick-level stop checks before any scheduled
work, then applies FX-day markers and bounded catch-up rules.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from atrade import market_fx, util  # noqa: E402

STATE_DIR = ROOT / "state_fx"
MARKER_NAME = "last_dispatch_fx.json"

# Inclusive minutes-from-midnight ET, intentionally wide for cron lateness.
WINDOWS = {
    "open": (8 * 60 + 5, 11 * 60),                 # 08:05-11:00
    "checkin_am": (3 * 60 + 5, 5 * 60 + 30),       # 03:05-05:30
    "checkin_pm": (14 * 60, 16 * 60 + 45),         # 14:00-16:45
    "preview": (18 * 60 + 5, 20 * 60 + 30),        # 18:05-20:30
    "week_ahead": (17 * 60 + 30, 19 * 60 + 30),    # Sunday 17:30-19:30
}

# Catch-up is limited to the associated session block. These are ET minutes.
AM_CATCHUP_END = 11 * 60 + 30       # London session end
OPEN_CATCHUP_END = 16 * 60 + 45     # stop taking catch-up entries before pre-break pass
PM_CATCHUP_END = 17 * 60            # market rollover / maintenance break


def minute_of_day(value=None) -> int:
    local = market_fx.as_et(value)
    return local.hour * 60 + local.minute


def in_window(value, window: tuple[int, int]) -> bool:
    """Inclusive window check used for deterministic boundary tests."""
    minute = minute_of_day(value)
    return int(window[0]) <= minute <= int(window[1])


def window_contains(name: str, value) -> bool:
    return in_window(value, WINDOWS[name])


def _marker_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / MARKER_NAME


def _load_marker(state_dir: str | Path, now) -> dict:
    label = market_fx.fx_day(now).isoformat()
    path = _marker_path(state_dir)
    try:
        marker = json.loads(path.read_text())
    except Exception:
        marker = {}
    if marker.get("fx_day") != label:
        marker = {"fx_day": label, "runs": {}}
    marker.setdefault("runs", {})
    return marker


def _save_marker(state_dir: str | Path, marker: dict) -> None:
    path = _marker_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    util.write_json(path, marker)


def _is_marked(marker: dict, name: str) -> bool:
    if os.environ.get("FORCE_DISPATCH_FX") == "1":
        return False
    return name in (marker.get("runs") or {})


def _positions(engine_api, state_dir: str | Path) -> list[dict]:
    try:
        return engine_api.fx_open_positions(state_dir) or []
    except Exception:
        try:
            data = json.loads((Path(state_dir) / "state.json").read_text())
            return [trade for trade in data.get("ledger", [])
                    if trade.get("symbol") == "XAUUSD" and trade.get("status") == "open"]
        except Exception:
            return []


def _had_entry(engine_api, state_dir: str | Path, fx_day: str) -> bool:
    try:
        return bool(engine_api.fx_had_entry(state_dir, fx_day))
    except Exception:
        try:
            data = json.loads((Path(state_dir) / "state.json").read_text())
            return any(trade.get("symbol") == "XAUUSD" and trade.get("fx_day") == fx_day
                       for trade in data.get("ledger", []))
        except Exception:
            return False


def _call_run(engine_api, state_dir, method: str, name: str,
              now, quote: dict | None, *, block: str | None = None,
              catchup: bool = False, notify: bool = True,
              sync_playbook: bool = True) -> dict:
    callback = getattr(engine_api, method)
    kwargs = {"now": now, "notify": notify}
    if method in {"swing_open_run", "risk_check_run"}:
        kwargs["price"] = quote
    if method == "risk_check_run":
        kwargs["block"] = block or name
    if method in {"session_preview_run", "fx_week_ahead_run"}:
        kwargs["summary"] = None
    if method in {"swing_open_run", "risk_check_run"}:
        kwargs["sync_playbook"] = sync_playbook
    result = callback(state_dir, **kwargs)
    result = result if isinstance(result, dict) else {"status": "ok"}
    result["dispatch_name"] = name
    result["catchup"] = bool(catchup)
    return result


def dispatch_tick(state_dir: str | Path = STATE_DIR, now=None, price=None, *,
                  engine_api=None, notify: bool = True,
                  sync_playbook: bool = True) -> dict:
    """Run one fake-clock-capable dispatch tick.

    ``engine_api`` and ``price`` are injectable for the fake-clock harness.
    A stop pass is attempted on every invocation, including break/weekend
    ticks; the engine declines market execution while the venue is closed.
    """
    from atrade import engine as default_engine

    engine_api = engine_api or default_engine
    local = market_fx.as_et(now)
    label = market_fx.fx_day(local).isoformat()
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = _load_marker(state_dir, local)
    marker["last_tick"] = {"at": local.isoformat(timespec="seconds"), "fx_day": label}
    actions: list[dict] = []

    # Safety pass runs before any windows/catch-up on every dispatcher tick.
    try:
        tick_result = engine_api.fx_tick(
            state_dir, now=local, price=price, notify=notify,
            sync_playbook=sync_playbook)
        tick_result = tick_result if isinstance(tick_result, dict) else {"status": "ok"}
    except Exception as exc:
        util.log(f"FX stop tick failed: {exc}", "ERROR")
        tick_result = {"status": "error", "error": str(exc)}
        actions.append({"name": "stop_tick", **tick_result})
    quote = None
    if tick_result.get("price") is not None:
        quote = {"price": tick_result["price"],
                 "symbol": tick_result.get("source") or "dispatch quote",
                 "at": tick_result.get("at") or local.isoformat(timespec="seconds")}

    def run(name: str, method: str, *, block=None, catchup=False):
        try:
            result = _call_run(engine_api, state_dir, method, name, local, quote,
                               block=block, catchup=catchup, notify=notify,
                               sync_playbook=sync_playbook)
        except Exception as exc:
            util.log(f"FX {name} failed: {exc}", "ERROR")
            result = {"status": "error", "error": str(exc), "dispatch_name": name,
                      "catchup": bool(catchup)}
        actions.append(result)
        if result.get("status") != "error":
            marker["runs"][name] = {"at": local.isoformat(timespec="seconds"),
                                    "status": result.get("status", "ok"),
                                    "catchup": bool(catchup)}
        return result

    # Sunday digest is informational and may run during the 17:00-18:00 break.
    if local.weekday() == 6 and window_contains("week_ahead", local):
        if not _is_marked(marker, "week_ahead"):
            run("week_ahead", "fx_week_ahead_run")

    if market_fx.is_fx_open(local):
        hm = minute_of_day(local)
        open_start, open_end = WINDOWS["open"]
        did_open = False
        if open_start <= hm <= open_end:
            if not _is_marked(marker, "open"):
                run("open", "swing_open_run")
                did_open = True
        elif (open_end < hm <= OPEN_CATCHUP_END and
              not _is_marked(marker, "open") and
              not _had_entry(engine_api, state_dir, label)):
            run("open", "swing_open_run", catchup=True)
            did_open = True

        am_start, am_end = WINDOWS["checkin_am"]
        if am_start <= hm <= am_end:
            if not _is_marked(marker, "checkin_am"):
                run("checkin_am", "risk_check_run", block="checkin_am")
        elif (am_end < hm <= AM_CATCHUP_END and
              not _is_marked(marker, "checkin_am") and _positions(engine_api, state_dir)):
            run("checkin_am", "risk_check_run", block="checkin_am", catchup=True)

        pm_start, pm_end = WINDOWS["checkin_pm"]
        if pm_start <= hm <= pm_end:
            if not _is_marked(marker, "checkin_pm"):
                run("checkin_pm", "risk_check_run", block="checkin_pm")
        elif (pm_end < hm < PM_CATCHUP_END and
              not _is_marked(marker, "checkin_pm") and _positions(engine_api, state_dir)):
            run("checkin_pm", "risk_check_run", block="checkin_pm", catchup=True)

        if window_contains("preview", local) and not _is_marked(marker, "preview"):
            run("preview", "session_preview_run")

    marker["last_tick"].update({"stop_status": tick_result.get("status"),
                               "price": tick_result.get("price"),
                               "source": tick_result.get("source")})
    _save_marker(state_dir, marker)
    return {"status": "ok" if tick_result.get("status") != "error" else "error",
            "now_et": local.isoformat(timespec="seconds"), "fx_day": label,
            "session": market_fx.fx_session(local), "tick": tick_result,
            "actions": actions, "marker": marker}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Dispatch the XAUUSD swing paper book")
    parser.add_argument("--state-dir", default=str(STATE_DIR), help="isolated FX state directory")
    parser.add_argument("--now", help="fake/local ET ISO timestamp (for test/replay only)")
    parser.add_argument("--price", type=float, help="injected price for a paper replay")
    parser.add_argument("--no-notify", action="store_true", help="suppress Telegram messages")
    parser.add_argument("--no-playbook-sync", action="store_true", help="do not update PLAYBOOK.md")
    args = parser.parse_args(argv)
    local = market_fx.as_et(args.now)
    util.configure_logging(Path(args.state_dir) / "logs")
    print(f"[dispatch-fx] {local.isoformat(timespec='minutes')} ET | "
          f"fx_day={market_fx.fx_day(local)} | session={market_fx.fx_session(local)}")
    result = dispatch_tick(
        args.state_dir, now=local, price=args.price,
        notify=not args.no_notify, sync_playbook=not args.no_playbook_sync)
    for action in result["actions"]:
        name = action.get("dispatch_name", "stop_tick")
        suffix = " catch-up" if action.get("catchup") else ""
        print(f"[dispatch-fx] {name}{suffix} -> {action.get('status')}")
    if not result["actions"]:
        print(f"[dispatch-fx] stop tick -> {result['tick'].get('status')} (no scheduled window)")
    return 1 if result["status"] == "error" else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
