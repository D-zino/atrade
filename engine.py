"""Engine: orchestrates open runs (research + open trades) and close runs
(research, close day trades, evaluate, learn, report)."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

from . import (broker as broker_mod, evaluator, indicators, learning, market,
               reporting, signals, state as state_mod, trading, util)


def _load_state_and_cfg(state_dir) -> tuple:
    util.configure_logging(Path(state_dir) / "logs")
    cfg = __import__("atrade.config", fromlist=["load_config"]).load_config()
    st = state_mod.State(state_dir)
    return st, cfg


def _prices_from_research(summary: dict) -> dict:
    p = summary.get("prices") or {}
    return {k: float(v.get("close")) for k, v in p.items() if v and v.get("close")}


def _prices_from_bars(broker, cfg: dict, fallback: dict) -> dict:
    out = dict(fallback)
    try:
        bars = broker.bars(cfg.get("universe", []), "1Day", 30)
        for sym, bl in bars.items():
            if bl:
                out[sym] = float(bl[-1].get("c") or bl[-1].get("close"))
    except Exception as e:
        util.log(f"bars unavailable ({e}); using research prices", "WARN")
    return out


def _shared_research(st, cfg, force_mock: bool, session: str = "open",
                     use_cached: bool = False) -> tuple:
    """Research summary + price_map + broker + mode + tech snapshots.

    use_cached=True replays research/latest.json (fast multi-day simulations).
    """
    from . import research as research_mod
    if use_cached:
        cached = st.dir / "research" / "latest.json"
        if cached.exists():
            summary = util.read_json(cached)
            util.log("using cached research summary (simulation mode)", "INFO")
        else:
            summary = research_mod.run_research(cfg, st.dir, cfg.get("universe", []))
    else:
        summary = research_mod.run_research(cfg, st.dir, cfg.get("universe", []))
    price_map = _prices_from_research(summary)
    broker, mode = _broker_for(cfg, st, price_map, force_mock, session=session)
    if mode == "alpaca_paper":
        price_map = _prices_from_bars(broker, cfg, price_map)
    if hasattr(broker, "seed_prices"):
        broker.seed_prices(price_map, session=session)
    # technicals: Alpaca bars if live-paper, else research (Yahoo) bars
    tech = {}
    if mode == "alpaca_paper":
        tech = _tech_snapshot(broker, cfg)
    if not tech:
        bars = summary.get("bars") or {}
        for sym, bl in bars.items():
            if len(bl) >= 20:
                snap = indicators.technical_snapshot(bl)
                if snap:
                    tech[sym] = snap
    tech_notes = indicators.technical_notes(tech, cfg.get("universe", []))
    summary["notes"] = (summary.get("notes") or []) + tech_notes
    # enriched scan universe (base + dynamic additions from news/momentum)
    dynamic = summary.get("dynamic") or {}
    scan_universe = dynamic.get("scan_universe") or cfg.get("universe", [])
    return summary, price_map, broker, mode, tech, scan_universe


def _broker_for(cfg, st, price_map=None, force_mock=False, session: str = "open"):
    return broker_mod.make_broker(cfg, st.dir, price_src=price_map, force_mock=force_mock,
                                  session=session)


def _tech_snapshot(broker, cfg: dict) -> dict:
    tech = {}
    try:
        bars = broker.bars(cfg.get("universe", []), "1Day", 60)
        for sym, bl in bars.items():
            snap = indicators.technical_snapshot(bl)
            if snap:
                tech[sym] = snap
    except Exception as e:
        util.log(f"technical snapshot failed: {e}", "WARN")
    return tech


def _equity_now(broker, cfg: dict, st) -> float:
    try:
        acct = broker.account()
        return float(acct.get("equity") or acct.get("portfolio_value") or 0.0)
    except Exception:
        net = sum((t.get("pnl") or 0) for t in st.ledger)
        return float(cfg.get("initial_equity", 100000.0)) + net


def _guard_weekday_holiday(cfg, st, allow_anyday: bool = False) -> str | None:
    """Returns a skip reason string, or None if we should run."""
    if allow_anyday:
        return None
    now = datetime.now(market.TZ)
    if cfg.get("weekdays_only") and now.weekday() >= 5:
        return "weekend (weekdays only)"
    if not market.is_trading_day(now.date()):
        return f"market holiday ({now.date()})"
    return None


def _drawdown_check(st, equity_now: float, cfg: dict) -> tuple[float, bool, str | None]:
    """Track peak equity; flag pause if equity falls below peak*(1 - max_drawdown_pct)."""
    peak = st.data.get("peak_equity") or equity_now
    new_peak = max(peak, equity_now)
    st.data["peak_equity"] = new_peak
    dd = cfg.get("max_drawdown_pct")
    if dd and new_peak > 0 and equity_now < new_peak * (1 - float(dd)):
        reason = (f"max drawdown exceeded: equity {equity_now:,.0f} vs peak {new_peak:,.0f} "
                  f"({(1 - equity_now / new_peak) * 100:.1f}% below peak, limit {float(dd) * 100:.0f}%)")
        return new_peak, True, reason
    return new_peak, False, None


def _daily_pnl(st, existing: list[dict]) -> float:
    """Today's realized P&L (trades closed today) + unrealized on open positions."""
    from datetime import date as _date
    today_iso = _date.today().isoformat()
    realized = sum((t.get("pnl") or 0) for t in st.ledger
                   if (t.get("closed_at") or "").startswith(today_iso))
    unrealized = sum(float(p.get("unrealized_pl") or 0) for p in existing)
    return realized + unrealized


def open_run(state_dir: str | Path, force_mock: bool = False, allow_anyday: bool = False,
             use_cached: bool = False, report_dir=None, run_tag: str = "") -> dict:
    st, cfg = _load_state_and_cfg(state_dir)
    if st.paused:
        util.log("PAUSED — skipping open run (see state.json resume).", "WARN")
        return {"status": "paused"}
    skip = _guard_weekday_holiday(cfg, st, allow_anyday)
    if skip:
        util.log(f"Skipping open run: {skip}", "INFO")
        return {"status": "skipped", "reason": skip}

    summary, price_map, broker, mode, tech, scan_universe = _shared_research(st, cfg, force_mock, session="open",
                                                                             use_cached=use_cached)

    # baseline capture on first run
    if not st.data.get("baseline"):
        base = evaluator.capture_baseline(price_map, st.dir, cfg.get("initial_equity", 100000.0))
        st.set_baseline(base)
        util.log(f"Baseline captured: SPY @ {base['benchmarks'].get('SPY', {}).get('price')}, "
                 f"QQQ @ {base['benchmarks'].get('QQQ', {}).get('price')}", "INFO")

    tracker = st.data.get("signal_tracker") or {}
    hyps = signals.build_hypotheses(summary, tech, tracker, scan_universe, cfg)
    ranked = signals.rank_signals(hyps)

    equity = _equity_now(broker, cfg, st)
    existing = broker.positions() if hasattr(broker, "positions") else []
    n_trades = len([t for t in st.ledger if t.get("status") in ("closed", "open")])
    wf = trading.warmup_factor(cfg, n_trades)
    if wf < 1.0:
        util.log(f"WARM-UP: {n_trades}/{cfg.get('warmup_until_trades', 12)} trades → "
                 f"sizing at {wf*100:.0f}% of normal", "INFO")
    daily_pnl = _daily_pnl(st, existing)
    opened, skipped = trading.open_positions(broker, cfg, ranked, equity, existing, price_map,
                                             warmup_factor_override=wf, daily_pnl=daily_pnl)
    if opened:
        st.add_trades(opened)

    st.data["last_open"] = {"at": util.utc_iso(), "opened": [t["symbol"] for t in opened],
                            "skipped": skipped, "hypotheses": len(hyps), "mode": mode}
    st.record_run({"type": "open", "at": util.utc_iso(), "opened": len(opened),
                   "hypotheses": len(hyps), "mode": mode})
    try:
        report = reporting.open_report(st.data, summary, hyps, opened, skipped, tech, mode)
        rdir = Path(report_dir) if report_dir else (st.dir / "reports")
        rpath = rdir / f"{run_tag}open_{date.today().isoformat()}.md"
        rpath.write_text(report)
    except Exception as e:
        util.log(f"open report failed: {e}", "ERROR")
        rpath = Path("")
    st.save()
    try:
        from . import telegram
        dyn = summary.get("dynamic") or {}
        adds = dyn.get("adds") or []
        msg = telegram.format_open(util.utc_iso(), mode, len(scan_universe),
                                   opened, skipped, hyps, adds=adds)
        telegram.send(msg)
    except Exception as e:
        util.log(f"telegram open notification failed: {e}", "WARN")
    util.log(f"OPEN RUN done: {len(opened)} opened, {len(hyps)} hypotheses. Report: {rpath.name}")
    return {"status": "ok", "opened": opened, "skipped": skipped, "hypotheses": len(hyps),
            "report_path": str(rpath), "mode": mode, "summary": summary, "hyps": hyps}


def close_run(state_dir: str | Path, force_mock: bool = False, allow_anyday: bool = False,
              use_cached: bool = False, report_dir=None, run_tag: str = "") -> dict:
    st, cfg = _load_state_and_cfg(state_dir)
    if st.paused:
        util.log("PAUSED — skipping close run.", "WARN")
        return {"status": "paused"}
    skip = _guard_weekday_holiday(cfg, st, allow_anyday)
    if skip:
        util.log(f"Skipping close run: {skip}", "INFO")
        return {"status": "skipped", "reason": skip}

    summary, price_map, broker, mode, tech, scan_universe = _shared_research(st, cfg, force_mock, session="close",
                                                                             use_cached=use_cached)
    tracker = st.data.get("signal_tracker") or {}
    hyps = signals.build_hypotheses(summary, tech, tracker, scan_universe, cfg)

    # ---- close day trades ----------------------------------------------------
    positions = broker.positions() if hasattr(broker, "positions") else []
    stop_closed = trading.intraday_stop_check(broker, cfg, st.ledger, positions, price_map)
    positions = broker.positions() if hasattr(broker, "positions") else []
    closed = trading.close_day_trades(broker, cfg, st.ledger, positions, price_map)
    all_closed = stop_closed + closed
    st.save()

    # ---- evaluate ------------------------------------------------------------
    equity_now = _equity_now(broker, cfg, st)
    peak, dd_paused, dd_reason = _drawdown_check(st, equity_now, cfg)
    base = st.data.get("baseline") or {}
    bench = evaluator.benchmark_return(price_map, base)
    eval_res = evaluator.evaluate_run(st.data, cfg, equity_now, bench)
    st.data["peak_equity"] = peak
    if dd_paused:
        eval_res["pause"] = True
        eval_res["pause_reason"] = dd_reason
    st.data["last_metrics"] = eval_res["metrics"]
    st.data["last_components"] = eval_res["components"]
    st.data["last_score"] = eval_res["score"]
    st.record_score({"at": util.utc_iso(), "score": eval_res["score"],
                     "metrics": eval_res["metrics"], "components": eval_res["components"],
                     "improved": eval_res["improved"]})
    if eval_res.get("pause"):
        reason = eval_res.get("pause_reason") or (
            f"no_improve_streak={eval_res['no_improve_streak']} | "
            f"failed_measure={eval_res['failed_measure_streak']}")
        st.mark_paused(reason)
        try:
            from . import telegram
            telegram.send(telegram.format_paused(reason,
                f"score {eval_res['score']}, best {eval_res['best_score']}"))
        except Exception as e:
            util.log(f"telegram pause notification failed: {e}", "WARN")

    # ---- learning loop ---------------------------------------------------------
    learn = learning.run_learning(all_closed, tracker, st.data, cfg, summary)
    st.data["signal_tracker"] = learn["tracker"]
    cfg = _apply_rule_updates(cfg, learn)
    st.data["lessons"] = learn["lessons"]
    st.record_thinking(_thinking_change(all_closed, learn, eval_res))
    st.data["last_close"] = {"at": util.utc_iso(), "closed": len(all_closed),
                             "pnl": eval_res["metrics"]["net_pnl"]}
    st.record_run({"type": "close", "at": util.utc_iso(), "closed": len(all_closed),
                   "score": eval_res["score"], "improved": eval_res["improved"], "mode": mode})

    # ---- playbook + report ------------------------------------------------------
    playbook = learning.playbook_body(learn["tracker"], learn["lessons"], all_closed, st.data, cfg)
    (st.dir.parent / "PLAYBOOK.md").write_text(playbook)
    try:
        report = reporting.close_report(st.data, summary, hyps, all_closed,
                                        broker.positions() if hasattr(broker, "positions") else [],
                                        learn["lessons"], learn["tracker"], eval_res, mode,
                                        st.data.get("last_open", {}).get("opened", []))
        rdir = Path(report_dir) if report_dir else (st.dir / "reports")
        rpath = rdir / f"{run_tag}close_{date.today().isoformat()}.md"
        rpath.write_text(report)
    except Exception as e:
        util.log(f"close report failed: {e}", "ERROR")
        rpath = Path("")
    st.save()
    try:
        from . import telegram
        msg = telegram.format_close(util.utc_iso(), mode, all_closed, eval_res["score"],
                                    eval_res["improved"], eval_res["metrics"]["net_pnl"],
                                    learn["lessons"], learn["tracker"], hyps)
        telegram.send(msg)
    except Exception as e:
        util.log(f"telegram close notification failed: {e}", "WARN")
    util.log(f"CLOSE RUN done: {len(all_closed)} closed, score {eval_res['score']}, "
             f"improved={eval_res['improved']}. Report: {rpath.name}")
    return {"status": "ok", "closed": all_closed, "score": eval_res["score"],
            "improved": eval_res["improved"], "lessons": learn["lessons"],
            "report_path": str(rpath), "mode": mode}


# ---------------------------------------------------------------------------
# Mid-session check-in + tomorrow preview (Telegram extras)
# ---------------------------------------------------------------------------
def _tech_from_summary(summary: dict, symbols) -> dict:
    tech = {}
    bars = summary.get("bars") or {}
    for sym in symbols:
        bl = bars.get(sym) or []
        if len(bl) >= 20:
            snap = indicators.technical_snapshot(bl)
            if snap:
                tech[sym] = snap
    return tech


def _lightweight_research(st, cfg):
    """Reuse the cached research summary (fast); run fresh if none exists."""
    from . import research as research_mod
    cached = st.dir / "research" / "latest.json"
    if cached.exists():
        return util.read_json(cached) or {}
    util.log("no cached research — running fresh research pass", "WARN")
    return research_mod.run_research(cfg, st.dir, cfg.get("universe", []))


def checkin_run(state_dir: str | Path, force_mock: bool = False, allow_anyday: bool = False) -> dict:
    """10:30 ET — mid-session check-in: open positions + events in play."""
    st, cfg = _load_state_and_cfg(state_dir)
    if st.paused:
        return {"status": "paused"}
    skip = _guard_weekday_holiday(cfg, st, allow_anyday)
    if skip:
        return {"status": "skipped", "reason": skip}
    summary = _lightweight_research(st, cfg)
    price_map = _prices_from_research(summary)
    broker, mode = _broker_for(cfg, st, price_map, force_mock, session="checkin")
    if mode == "alpaca_paper":
        price_map = _prices_from_bars(broker, cfg, price_map)
    if hasattr(broker, "seed_prices"):
        broker.seed_prices(price_map, session="checkin")
    positions = broker.positions() if hasattr(broker, "positions") else []
    notes = summary.get("notes") or []
    strong = sorted([n for n in notes if (n.get("strength") or 0) >= 0.5],
                    key=lambda n: -(n.get("strength") or 0))[:4]
    try:
        from . import telegram
        telegram.send(telegram.format_checkin(util.utc_iso(), mode, positions, strong))
    except Exception as e:
        util.log(f"telegram check-in failed: {e}", "WARN")
    st.record_run({"type": "checkin", "at": util.utc_iso(), "mode": mode})
    st.save()
    util.log(f"CHECK-IN done: {len(positions)} positions, mode={mode}")
    return {"status": "ok", "positions": len(positions), "mode": mode}


def upcoming_events(d) -> list[str]:
    """Approximate upcoming-events calendar for a trading day (best-effort)."""
    events = []
    if d.weekday() == 3:  # Thursday
        events.append("08:30 ET — Initial Jobless Claims")
    if d.weekday() == 4 and d.day <= 7:  # first Friday
        events.append("08:30 ET — Nonfarm Payrolls (NFP)")
    # 2026 FOMC decision days (standard 8-meeting cadence — approx.)
    fomc = {date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
            date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9)}
    if d in fomc:
        events.append("14:00 ET — FOMC rate decision & press conference (schedule approx.)")
    if not events:
        events.append("No major scheduled releases — watch for Fed speakers & Treasury auctions")
    return events


def preview_run(state_dir: str | Path, force_mock: bool = False, allow_anyday: bool = False) -> dict:
    """20:00 ET — tomorrow's preview: calendar + prior-adjusted watchlist."""
    st, cfg = _load_state_and_cfg(state_dir)
    if st.paused:
        return {"status": "paused"}
    skip = _guard_weekday_holiday(cfg, st, allow_anyday)
    if skip:
        return {"status": "skipped", "reason": skip}
    summary = _lightweight_research(st, cfg)
    tech = _tech_from_summary(summary, cfg.get("universe", []))
    tracker = st.data.get("signal_tracker") or {}
    scan_universe = (summary.get("dynamic") or {}).get("scan_universe") or cfg.get("universe", [])
    hyps = signals.build_hypotheses(summary, tech, tracker, scan_universe, cfg)
    hyps.sort(key=lambda h: -h["confidence"])
    next_day = market.next_trading_day(date.today())
    events = upcoming_events(next_day)
    try:
        from . import telegram
        telegram.send(telegram.format_preview(util.utc_iso(), market.date_str(next_day), events, hyps))
    except Exception as e:
        util.log(f"telegram preview failed: {e}", "WARN")
    st.record_run({"type": "preview", "at": util.utc_iso(), "hypotheses": len(hyps)})
    st.save()
    util.log(f"PREVIEW done: {len(hyps)} hypotheses for {market.date_str(next_day)}")
    return {"status": "ok", "hypotheses": len(hyps), "next_day": market.date_str(next_day)}


def upcoming_week_events(days: list) -> list[str]:
    """Approximate next-week macro/earnings calendar (best-effort, labeled approx)."""
    events = []
    fomc_2026 = {date(2026, 1, 28), date(2026, 3, 18), date(2026, 4, 29), date(2026, 6, 17),
                 date(2026, 7, 29), date(2026, 9, 16), date(2026, 10, 28), date(2026, 12, 9)}
    earnings_months = (1, 4, 7, 10)
    for d in days:
        if d.weekday() == 3:
            events.append(f"{d.strftime('%a %b %d')} — 08:30 ET Jobless Claims")
        if d in fomc_2026:
            events.append(f"{d.strftime('%a %b %d')} — 14:00 ET FOMC decision + presser")
        if d.weekday() == 4 and d.day <= 7:
            events.append(f"{d.strftime('%a %b %d')} — 08:30 ET Nonfarm Payrolls (NFP)")
        if d.weekday() == 4 and 15 <= d.day <= 21:
            events.append(f"{d.strftime('%a %b %d')} — Triple witching (options/futures expiry)")
        if d.month in earnings_months and 12 <= d.day <= 18:
            events.append(f"{d.strftime('%a %b %d')} — Bank earnings window (JPM, BAC, GS, WFC)")
        elif d.month in earnings_months and 22 <= d.day <= 31:
            events.append(f"{d.strftime('%a %b %d')} — Megacap tech earnings window (MSFT, GOOGL, META, AMZN)")
    if any(9 <= d.day <= 15 for d in days):
        events.append("CPI likely this week (BLS mid-month window, 08:30 ET)")
    if any(14 <= d.day <= 18 for d in days):
        events.append("PPI likely this week (mid-month window)")
    if not events:
        events.append("No major scheduled macro releases flagged this week")
    return events


def week_ahead_run(state_dir: str | Path, force_mock: bool = False, allow_anyday: bool = False) -> dict:
    """Sunday 17:00 ET — week-ahead digest (informational; sends even if paused)."""
    st, cfg = _load_state_and_cfg(state_dir)
    paused = st.paused
    summary = _lightweight_research(st, cfg)
    tech = _tech_from_summary(summary, cfg.get("universe", []))
    tracker = st.data.get("signal_tracker") or {}
    scan_universe = (summary.get("dynamic") or {}).get("scan_universe") or cfg.get("universe", [])
    hyps = signals.build_hypotheses(summary, tech, tracker, scan_universe, cfg)
    hyps.sort(key=lambda h: -h["confidence"])
    # next trading week (Mon–Fri)
    today = date.today()
    days_ahead = (0 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    monday = today + timedelta(days=days_ahead)
    week_days = [monday + timedelta(days=i) for i in range(5)]
    events = upcoming_week_events(week_days)
    fred = summary.get("fred") or {}
    paused_note = ("⚠️ Agent is currently PAUSED (auto-stop). This digest is for your "
                   "review — resume with: python3 -m atrade.cli resume") if paused else None
    try:
        from . import telegram
        telegram.send(telegram.format_week_ahead(util.utc_iso(), week_days, events, hyps,
                                                 fred, paused_note))
    except Exception as e:
        util.log(f"telegram week-ahead failed: {e}", "WARN")
    st.record_run({"type": "week_ahead", "at": util.utc_iso(), "hypotheses": len(hyps),
                   "week_start": market.date_str(monday)})
    st.save()
    util.log(f"WEEK-AHEAD done: {len(hyps)} hypotheses for week of {market.date_str(monday)}")
    return {"status": "ok", "hypotheses": len(hyps), "week_start": market.date_str(monday),
            "week_days": [market.date_str(d) for d in week_days]}


def _apply_rule_updates(cfg: dict, learn: dict) -> dict:
    """Tweak active rules from lessons (the 'evolving rules' bit).

    A discount rule is only self-imposed with meaningful sample evidence
    (>= 3 graded trades in the category AND win rate < 40%) — otherwise the
    loop would thrash rules on single-trade noise.
    """
    rules = list(cfg.get("active_rules") or learning._default_rules())
    tracker = learn.get("tracker") or {}
    for l in learn.get("lessons") or []:
        if l.startswith("Losers were dominated by"):
            import re
            m = re.search(r"dominated by '(\w+)'", l)
            if not m:
                continue
            cat = m.group(1)
            st = tracker.get(cat) or {}
            if (st.get("n") or 0) >= 3 and (st.get("win_rate") or 1.0) < 0.40:
                if not any(cat in r for r in rules):
                    rules.append(f"Discount '{cat}' evidence until its signal-tracker win rate "
                                 f"recovers above 50% (self-imposed by the learning loop).")
    cfg["active_rules"] = rules
    return cfg


def _thinking_change(closed: list[dict], learn: dict, eval_res: dict) -> str:
    if not closed:
        return ("No trades closed today; priors unchanged but fresh research was logged. "
                "The playbook still lacks outcome evidence for this setup class.")
    wins = sum(1 for t in closed if (t.get("pnl") or 0) > 0)
    cats = {}
    for t in closed:
        c = (t.get("hypothesis") or {}).get("dominant_category", "?")
        cats[c] = cats.get(c, 0) + 1
    top = max(cats, key=cats.get)
    verdict = "confirmed my edge" if wins > len(closed) / 2 else "refuted my edge"
    return (f"After {len(closed)} graded trades ({wins} wins), the '{top}' signal family "
            f"{verdict}. Composite score {eval_res['score']:.3f} "
            f"({'improved' if eval_res.get('improved') else 'did not improve'}). "
            f"I will {'trust' if wins > len(closed)/2 else 'discount'} '{top}' evidence tomorrow.")


# ---------------------------------------------------------------------------
# Status / maintenance
# ---------------------------------------------------------------------------
def simulate(state_dir: str | Path, days: int = 5) -> dict:
    """Fast multi-day simulation: N open/close pairs on the mock broker using
    cached research data. Lets the self-improvement loop accumulate evidence
    and evolve the playbook without waiting for real trading days."""
    st, cfg = _load_state_and_cfg(state_dir)
    # ensure we have cached research
    if not (st.dir / "research" / "latest.json").exists():
        from . import research as research_mod
        util.log("no cached research — running one live research pass first", "INFO")
        research_mod.run_research(cfg, st.dir, cfg.get("universe", []))
    if not st.data.get("baseline"):
        summary = util.read_json(st.dir / "research" / "latest.json")
        price_map = _prices_from_research(summary or {})
        st.set_baseline(evaluator.capture_baseline(price_map, st.dir, cfg.get("initial_equity", 100000.0)))
        st.save()
    sim_dir = st.dir / "reports" / "sim"
    sim_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for day in range(1, days + 1):
        util.log(f"=== SIM DAY {day}/{days} (open) ===", "INFO")
        ro = open_run(state_dir, force_mock=True, allow_anyday=True, use_cached=True,
                      report_dir=sim_dir, run_tag=f"day{day:02d}")
        util.log(f"=== SIM DAY {day}/{days} (close) ===", "INFO")
        rc = close_run(state_dir, force_mock=True, allow_anyday=True, use_cached=True,
                       report_dir=sim_dir, run_tag=f"day{day:02d}")
        results.append({"day": day, "opened": len(ro.get("opened") or []),
                        "closed": len(rc.get("closed") or []),
                        "score": rc.get("score"), "improved": rc.get("improved"),
                        "lessons": rc.get("lessons")})
        util.log(f"SIM DAY {day} done: score {rc.get('score')}, improved={rc.get('improved')}", "INFO")
    # refresh artifacts
    write_playbook_skeleton(state_dir)
    try:
        from . import dashboard
        (st.dir.parent / "dashboard.html").write_text(dashboard.build())
    except Exception:
        pass
    return {"status": "ok", "days": results}


def status(state_dir: str | Path) -> dict:
    st, cfg = _load_state_and_cfg(state_dir)
    now = datetime.now(market.TZ)
    sched = market.next_schedule(util.now_utc(), cfg.get("open_run_time_et"),
                                 cfg.get("close_run_time_et"), cfg.get("early_close_time_et"))
    return {
        "now_et": now.isoformat(timespec="minutes"),
        "market": market.market_status(now),
        "mode": cfg.get("broker"),
        "paused": st.paused,
        "pause_reason": st.data.get("resume", {}).get("reason"),
        "next_runs": sched,
        "n_runs": len(st.data.get("runs", [])),
        "n_trades": len(st.ledger),
        "last_score": st.data.get("last_score"),
        "best_score": max((h.get("score") for h in st.data.get("score_history", [])
                           if h.get("score") is not None), default=None),
        "streaks": st.data.get("streaks", {}),
        "open_trades": [{"symbol": t.get("symbol"), "side": t.get("side"),
                         "qty": t.get("qty"), "entry": t.get("entry_price")}
                        for t in st.ledger if t.get("status") == "open"],
        "warmup": {"trades": len([t for t in st.ledger if t.get("status") in ("closed", "open")]),
                   "until": cfg.get("warmup_until_trades", 12),
                   "factor": trading.warmup_factor(cfg, len([t for t in st.ledger
                                                             if t.get("status") in ("closed", "open")]))},
        "risk": {"max_positions": cfg.get("max_positions", 2),
                 "max_position_pct": cfg.get("max_position_pct", 0.12),
                 "max_portfolio_pct": cfg.get("max_portfolio_pct", 0.24),
                 "intraday_stop_pct": cfg.get("intraday_stop_pct", 0.014),
                 "daily_loss_limit_pct": cfg.get("daily_loss_limit_pct", 0.03),
                 "max_drawdown_pct": cfg.get("max_drawdown_pct", 0.10),
                 "peak_equity": st.data.get("peak_equity")},
    }


def resume(state_dir: str | Path) -> dict:
    st, _ = _load_state_and_cfg(state_dir)
    if not st.paused:
        return {"status": "already_running"}
    st.resume()
    st.save()
    return {"status": "resumed"}


def write_playbook_skeleton(state_dir: str | Path) -> None:
    st, cfg = _load_state_and_cfg(state_dir)
    tracker = st.data.get("signal_tracker") or {}
    body = learning.playbook_body(tracker, ["Bootstrap: no graded trades yet — signal tracker is empty until "
                                            "the first close run produces outcomes."], [], st.data, cfg)
    (st.dir.parent / "PLAYBOOK.md").write_text(body)
    st.save()
    return {"status": "ok", "playbook": str(st.dir.parent / "PLAYBOOK.md")}
