#!/usr/bin/env python3
"""A-Trade CLI: the autonomous paper-trading agent entry point.

Usage:
  python -m atrade.cli open                    # open run (research + open trades)
  python -m atrade.cli close                   # close run (close, evaluate, learn, report)
  python -m atrade.cli status                  # status: market state, next runs, scores
  python -m atrade.cli resume                  # resume after auto-pause
  python -m atrade.cli playbook                # (re)write PLAYBOOK.md skeleton
  python -m atrade.cli report --run close --date YYYY-MM-DD   # print a report
  python -m atrade.cli watch [--interval 60] [--mock]  # blocking scheduler loop

Environment:
  ATRADE_STATE_DIR   override state dir (default: atrade/state)
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

from . import market, util


def _state_dir_arg() -> str:
    import os
    return os.environ.get("ATRADE_STATE_DIR") or str(Path(__file__).resolve().parent.parent / "state")


def cmd_open(args) -> int:
    from . import engine
    r = engine.open_run(_state_dir_arg(), force_mock=args.mock, allow_anyday=args.anyday)
    print(f"open run: {r.get('status')}")
    if r.get("report_path"):
        print(f"report: {r['report_path']}")
    if r.get("opened"):
        for t in r["opened"]:
            print(f"  OPEN {t['side'].upper()} {t['qty']} {t['symbol']} @ {t['entry_price']}")
    if r.get("skipped"):
        print("  skipped:", "; ".join(r["skipped"][:5]))
    return 0


def cmd_close(args) -> int:
    from . import engine
    r = engine.close_run(_state_dir_arg(), force_mock=args.mock, allow_anyday=args.anyday)
    print(f"close run: {r.get('status')}")
    if r.get("report_path"):
        print(f"report: {r['report_path']}")
    for t in r.get("closed") or []:
        print(f"  CLOSED {t['symbol']} {t.get('pnl'):+.2f} ({t.get('pnl_pct',0)*100:+.2f}%) "
              f"{'✅' if t.get('hypothesis_correct') else '❌'}")
    if r.get("score") is not None:
        print(f"  composite score: {r['score']} (improved: {r.get('improved')})")
    for l in r.get("lessons") or []:
        print(f"  lesson: {l}")
    return 0


def cmd_status(args) -> int:
    from . import engine
    s = engine.status(_state_dir_arg())
    print("=" * 60)
    print(f"Now (ET):     {s['now_et']}  market: {s['market']}")
    print(f"Mode:         {s['mode']}")
    print(f"Paused:       {s['paused']}" + (f"  ({s['pause_reason']})" if s.get("pause_reason") else ""))
    print(f"Runs:         {s['n_runs']}   Trades in ledger: {s['n_trades']}")
    print(f"Last score:   {s['last_score']}   Best: {s['best_score']}")
    print(f"Streaks:      {s['streaks']}")
    w = s.get("warmup") or {}
    if w:
        until = w.get("until", 12)
        if w.get("trades", 0) < until:
            print(f"Warm-up:      {w['trades']}/{until} trades done — sizing at "
                  f"{w['factor']*100:.0f}% of normal (anti-overfitting)")
        else:
            print(f"Warm-up:      complete ({w['trades']} trades) — full sizing")
    ni = (s.get("streaks") or {}).get("no_improve", 0)
    print(f"Pause guard:  no-improve streak {ni}/6 "
          f"{'(would pause in ' + str(6 - ni) + ' more flat runs)' if ni < 6 else '— PAUSED'}")
    print("Next scheduled runs:")
    for r in s['next_runs']:
        print(f"  [{r['type']:5}] {r['at_et']} ET  ({r['at_utc']} UTC)")
    print("Open trades:")
    for t in s['open_trades']:
        print(f"  {t['symbol']} {t['side']} qty={t['qty']} entry={t['entry']}")
    if not s['open_trades']:
        print("  (none)")
    return 0


def cmd_resume(args) -> int:
    from . import engine
    r = engine.resume(_state_dir_arg())
    print(r)
    return 0


def cmd_playbook(args) -> int:
    from . import engine
    r = engine.write_playbook_skeleton(_state_dir_arg())
    print(r)
    return 0


def cmd_universe(args) -> int:
    """Show or set the scanned universe (persists to state/config.json).

    Usage:
      python3 -m atrade.cli universe                     # show current
      python3 -m atrade.cli universe "NVDA AMD META"     # set new list
    """
    from . import engine
    st, cfg = engine._load_state_and_cfg(_state_dir_arg())
    if args.symbols:
        syms = [s.strip().upper() for s in args.symbols.split() if s.strip()]
        if len(syms) < 2:
            print("need at least 2 symbols")
            return 1
        overrides = util.read_json(st.dir / "config.json", {}) or {}
        overrides["universe"] = syms
        util.write_json(st.dir / "config.json", overrides)
        print(f"universe set to {len(syms)} symbols: {', '.join(syms)}")
        print("(max_positions still limits only how many trades are OPENED, not scanned)")
    else:
        print(f"current universe ({len(cfg.get('universe', []))} symbols):")
        print("  " + ", ".join(cfg.get("universe", [])))
        print()
        print("expand with:  python3 -m atrade.cli universe \"SYM1 SYM2 ...\"")
    return 0


def cmd_notify(args) -> int:
    """Send a test Telegram notification (needs TELEGRAM_* in .env)."""
    from . import telegram
    if not telegram.configured():
        print("Telegram is not configured. Add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env")
        return 1
    text = args.text or ("<b>✅ A-Trade test notification</b>\n\nIf you can read this, "
                         "your Telegram alerts are live. You'll get open/close run summaries "
                         "each trading day plus the watchlist.")
    ok = telegram.send(text)
    print("sent" if ok else "failed to send")
    return 0 if ok else 1


def cmd_dashboard(args) -> int:
    from . import dashboard
    root = Path(__file__).resolve().parent.parent
    out = root / "dashboard.html"
    out.write_text(dashboard.build())
    print(f"wrote {out}")
    return 0


def cmd_ledger(args) -> int:
    """Materialize trades_ledger.json at the project root (every trade +
    hypothesis + confidence + outcome) for easy inspection/export."""
    from . import engine, util
    st, cfg = engine._load_state_and_cfg(_state_dir_arg())
    root = Path(__file__).resolve().parent.parent
    data = {
        "generated_at": util.utc_iso(),
        "mode": cfg.get("broker"),
        "baseline": st.data.get("baseline"),
        "last_score": st.data.get("last_score"),
        "best_score": max((h.get("score") for h in st.data.get("score_history", [])
                           if h.get("score") is not None), default=None),
        "trades": st.ledger,
    }
    util.write_json(root / "trades_ledger.json", data)
    print(f"wrote {root / 'trades_ledger.json'} ({len(st.ledger)} trades)")
    return 0


def cmd_report(args) -> int:
    from . import util
    from datetime import date
    st_dir = Path(_state_dir_arg()) / "reports"
    d = args.date or date.today().isoformat()
    p = st_dir / f"{args.run}_{d}.md"
    if not p.exists():
        print(f"no report found: {p}")
        return 1
    print(p.read_text())
    return 0


def cmd_checkin(args) -> int:
    """Send a mid-session check-in now (Telegram)."""
    from . import engine
    r = engine.checkin_run(_state_dir_arg(), force_mock=args.mock, allow_anyday=args.anyday)
    print(f"check-in: {r.get('status')}")
    return 0


def cmd_preview(args) -> int:
    """Send a tomorrow-preview now (Telegram)."""
    from . import engine
    r = engine.preview_run(_state_dir_arg(), force_mock=args.mock, allow_anyday=args.anyday)
    print(f"preview: {r.get('status')} (next day: {r.get('next_day', 'n/a')})")
    return 0


def cmd_weekahead(args) -> int:
    """Send the Sunday week-ahead digest now (Telegram)."""
    from . import engine
    r = engine.week_ahead_run(_state_dir_arg(), force_mock=args.mock, allow_anyday=args.anyday)
    print(f"week-ahead: {r.get('status')} (week of {r.get('week_start', 'n/a')})")
    return 0


def cmd_sim(args) -> int:
    """Fast multi-day simulation on the mock broker (no network research)."""
    from . import engine
    r = engine.simulate(_state_dir_arg(), days=args.days)
    print(f"simulation done: {len(r.get('days', []))} days")
    for d in r.get("days", []):
        print(f"  day {d['day']}: opened {d['opened']}, closed {d['closed']}, "
              f"score {d['score']}, improved={d['improved']}")
    return 0


def cmd_watch(args) -> int:
    """Blocking scheduler: runs open/close at the scheduled ET times."""
    from . import engine
    cfg = __import__("atrade.config", fromlist=["load_config"]).load_config()
    interval = args.interval
    util.log(f"watch loop started (interval {interval}s, mock={args.mock})")
    while True:
        try:
            now = datetime.now(market.TZ)
            st = engine.status(_state_dir_arg())
            if not st["paused"]:
                for r in st["next_runs"][:1]:
                    if r["type"] == "open" and now.hour == 9 and now.minute >= 25 and now.minute < 40:
                        engine.open_run(_state_dir_arg(), force_mock=args.mock, allow_anyday=args.anyday)
                    elif r["type"] == "close" and now.hour == 15 and now.minute >= 50:
                        engine.close_run(_state_dir_arg(), force_mock=args.mock, allow_anyday=args.anyday)
                        engine.write_playbook_skeleton(_state_dir_arg())
            else:
                util.log("paused — waiting for resume")
        except Exception as e:
            util.log(f"watch loop error: {e}", "ERROR")
        time.sleep(interval)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="atrade", description="Autonomous paper-trading agent")
    ap.add_argument("command", choices=["open", "close", "status", "resume", "playbook",
                                        "report", "ledger", "watch", "sim", "dashboard",
                                        "notify", "universe", "checkin", "preview",
                                        "weekahead"])
    ap.add_argument("--mock", action="store_true", help="force offline mock broker (dry-run)")
    ap.add_argument("--anyday", action="store_true", help="bypass weekday/holiday calendar (for demos/tests)")
    ap.add_argument("--run", choices=["open", "close"], help="report: which run type")
    ap.add_argument("--date", help="report: YYYY-MM-DD")
    ap.add_argument("--interval", type=int, default=60, help="watch: poll interval seconds")
    ap.add_argument("--days", type=int, default=5, help="sim: number of simulated trading days")
    ap.add_argument("--text", help="notify: custom message to send")
    ap.add_argument("symbols", nargs="*", help="universe: new symbol list (space separated)")
    args = ap.parse_args(argv)
    return {"open": cmd_open, "close": cmd_close, "status": cmd_status,
            "resume": cmd_resume, "playbook": cmd_playbook, "report": cmd_report,
            "ledger": cmd_ledger, "watch": cmd_watch, "sim": cmd_sim,
            "dashboard": cmd_dashboard, "notify": cmd_notify,
            "universe": cmd_universe, "checkin": cmd_checkin,
            "preview": cmd_preview, "weekahead": cmd_weekahead}[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
