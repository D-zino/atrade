"""Engine: orchestrates open runs (research + open trades) and close runs
(research, close day trades, evaluate, learn, report)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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


def _today_et():
    """Which *trading* day is it? Answer in exchange time, never in UTC.

    The runner clock is UTC, so from 20:00 ET to midnight the UTC calendar
    has already rolled to tomorrow while the US session is still today. Any
    day-labelling (report filenames, P&L buckets, "next trading day") must
    use this, or evening runs silently file themselves under the next day.
    """
    return datetime.now(market.TZ).date()


def _closed_day_et(trade: dict):
    """ET calendar day a trade was closed on, or None if unparseable."""
    raw = trade.get("closed_at") or ""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(market.TZ).date()


def _daily_pnl(st, existing: list[dict]) -> float:
    """Today's realized P&L (trades closed today) + unrealized on open positions.

    Buckets realized P&L by the ET trading day instead of string-prefixing the
    UTC date: a position closed at 20:00 ET belongs to that session's P&L even
    though the runner's UTC clock has already rolled over to the next date.
    """
    today = _today_et()
    realized = sum((t.get("pnl") or 0) for t in st.ledger
                   if _closed_day_et(t) == today)
    unrealized = sum(float(p.get("unrealized_pl") or 0) for p in existing)
    return realized + unrealized


def _research_health(summary: dict | None, price_map: dict | None) -> dict:
    """Did the research fetch actually return anything usable?

    No notes *and* no prices means the fetch failed (timeouts, DNS, upstream
    5xx) — any "0 hypotheses" from that run is a measurement failure, not a
    flat market, and the caller should retry. Notes present but nothing
    clearing min_confidence is a legitimately quiet tape.
    """
    summary = summary or {}
    notes = summary.get("notes") or []
    return {"ok": bool(notes) or bool(price_map), "notes": len(notes),
            "prices": len(price_map or {})}


def open_run(state_dir: str | Path, force_mock: bool = False, allow_anyday: bool = False,
             use_cached: bool = False, report_dir=None, run_tag: str = "",
             notify_none: bool = True) -> dict:
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
    research = _research_health(summary, price_map)
    if not research["ok"]:
        util.log(f"research fetch came back empty ({research['notes']} notes, "
                 f"{research['prices']} prices) — this run cannot judge the tape", "WARN")

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
        rpath = rdir / f"{run_tag}open_{_today_et().isoformat()}.md"
        rpath.write_text(report)
    except Exception as e:
        util.log(f"open report failed: {e}", "ERROR")
        rpath = Path("")
    st.save()
    notified = False
    try:
        from . import telegram
        dyn = summary.get("dynamic") or {}
        adds = dyn.get("adds") or []
        msg = telegram.format_open(util.utc_iso(), mode, len(scan_universe),
                                   opened, skipped, hyps, adds=adds)
        if opened or notify_none:
            notified = bool(telegram.send(msg))
        else:
            # Retried open (research was empty on an earlier attempt): the
            # "Opened today: none" alert was already delivered today — don't
            # spam the chat on every retry tick.
            util.log("open-run 'none' notice already sent today — suppressed", "INFO")
    except Exception as e:
        util.log(f"telegram open notification failed: {e}", "WARN")
    util.log(f"OPEN RUN done: {len(opened)} opened, {len(hyps)} hypotheses. Report: {rpath.name}")
    return {"status": "ok", "opened": opened, "skipped": skipped, "hypotheses": len(hyps),
            "report_path": str(rpath), "mode": mode, "summary": summary, "hyps": hyps,
            "research": research, "notified": notified}


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
        rpath = rdir / f"{run_tag}close_{_today_et().isoformat()}.md"
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
    try:
        positions = broker.positions() if hasattr(broker, "positions") else []
        if not isinstance(positions, list):
            positions = []
    except Exception as e:
        util.log(f"positions unavailable ({e}); assuming flat", "WARN")
        positions = []
    notes = summary.get("notes") or []
    strong = sorted(
        [n for n in notes if isinstance(n, dict) and (n.get("strength") or 0) >= 0.5],
        key=lambda n: -(n.get("strength") or 0),
    )[:4]
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
    # ET "today": the preview fires 20:00–22:30 ET, when the runner's UTC clock
    # is already on tomorrow's date — date.today() would then preview the day
    # AFTER tomorrow (Thu 21:00 ET previewing Mon instead of Fri).
    next_day = market.next_trading_day(_today_et())
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
    # next trading week (Mon–Fri), anchored on the ET day: in winter the
    # Sunday 17:00–19:00 ET window straddles 00:00 UTC.
    today = _today_et()
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


# ---------------------------------------------------------------------------
# Isolated XAUUSD swing-with-stops book (additive; equity runs above unchanged)
# ---------------------------------------------------------------------------

def _fx_context(state_dir):
    """Load the FX ledger and its book-local configuration."""
    from . import config as config_mod
    loader = getattr(config_mod, "load_fx_config", None)
    cfg = loader(state_dir) if loader else {}
    util.configure_logging(Path(state_dir) / "logs")
    st = state_mod.State(state_dir)
    return st, cfg


def _fx_iso(now) -> str:
    from . import market_fx
    return market_fx.as_et(now).isoformat(timespec="seconds")


def _fx_quote(value, source: str = "injected", now=None) -> dict | None:
    """Normalize an injected test/dispatch price without consulting a live API."""
    if value is None:
        return None
    if isinstance(value, dict):
        raw = value.get("price")
        source = value.get("symbol") or value.get("source") or source
        stamp = value.get("at") or _fx_iso(now)
    else:
        raw, stamp = value, _fx_iso(now)
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    if price <= 0:
        return None
    return {"price": price, "symbol": str(source), "at": str(stamp)}


def _fetch_fx_quote(cfg: dict, now=None) -> dict | None:
    """Prefer Yahoo spot XAUUSD=X; fall back to the existing GC=F reference."""
    max_age = float(cfg.get("max_quote_age_minutes", 30.0))
    for symbol in (cfg.get("price_feed_primary", "XAUUSD=X"),
                   cfg.get("price_feed_fallback", "GC=F")):
        quote = broker_mod.fetch_yahoo_spot(symbol, now=now, max_age_minutes=max_age)
        if quote:
            return {"price": quote["price"], "symbol": symbol, "at": quote["at"]}
    return None


def _fx_broker(st, cfg: dict, quote: dict | None, session: str,
               drift_scale: float | None = None):
    """Always create the isolated fractional-oz MockBroker; never a live adapter."""
    prices = {"XAUUSD": float(quote["price"])} if quote else {}
    paper = broker_mod.MockBroker(
        st.dir,
        initial_equity=float(cfg.get("initial_equity", 100000.0)),
        slippage_bps=float(cfg.get("slippage_bps", 2.0)),
        price_src=prices,
        session=session,
        spread=float(cfg.get("spread", 0.30)),
        fractional_qty=True,
        account_filename=str(cfg.get("mock_account_filename", "mock_account_fx.json")),
        drift_scale=float(cfg.get("mock_drift_scale", 0.25) if drift_scale is None else drift_scale),
    )
    # Session/entry simulations advance the deterministic drift step. Stop and
    # scheduled risk checks use the injected live reference exactly as quoted.
    if quote and not (session.startswith("tick_stop") or session.startswith("risk_check") or
                      session == "thesis_broken"):
        paper.seed_prices(prices, session=session)
    return paper


def _fx_research_config(cfg: dict) -> dict:
    """Narrow the existing research collectors to gold without scanning equities."""
    return {
        "universe": ["XAUUSD"],
        "candidate_pool": [],
        "fred_series": cfg.get("fred_series", ["DFII10", "DTWEXBGS", "DGS10", "DGS2"]),
        "sec_enabled": False,
        "rss_queries": cfg.get("rss_queries", []),
        "auto_dynamic_universe": False,
        "max_dynamic_additions": 0,
        "momentum_top_n": 0,
        "rotation_bias_top": 0,
        "min_dynamic_volume": 0,
        "min_confidence": cfg.get("min_confidence", 0.60),
        "sector_of": {},
    }


def _fx_append_research_note(notes: list, note: dict) -> None:
    key = (note.get("source"), note.get("title"), tuple(note.get("tickers") or []))
    if not any(isinstance(existing, dict) and
               (existing.get("source"), existing.get("title"),
                tuple(existing.get("tickers") or [])) == key for existing in notes):
        notes.append(note)


def _fx_completed_bars(bars: list[dict], now) -> list[dict]:
    """Exclude a same-day partial candle when timestamps are available."""
    from . import market_fx
    local_day = market_fx.as_et(now).date().isoformat()
    dated, has_date = [], False
    for bar in bars or []:
        raw = str(bar.get("t") or bar.get("date") or "")
        if raw:
            has_date = True
            if raw[:10] < local_day:
                dated.append(bar)
    if has_date:
        return dated
    # Unstamped fixtures/feeds: conservatively exclude the last (possibly
    # still-forming) candle when enough history remains.
    return list(bars[:-1]) if len(bars or []) > 3 else list(bars or [])


def _fx_research(st, cfg: dict, now=None, summary: dict | None = None,
                 use_cached: bool = False) -> dict:
    """Collect gold/macro research and expose gold-only hypotheses and bars."""
    from . import market_fx
    if summary is None and use_cached:
        cached = st.dir / "research" / "latest.json"
        if cached.exists():
            summary = util.read_json(cached) or {}
    if summary is None:
        from . import research as research_mod
        summary = research_mod.run_research(_fx_research_config(cfg), st.dir, ["XAUUSD"])
        # Yahoo's spot symbol is optional. GC=F is already collected by the
        # existing research pass and remains the reference fallback.
        try:
            spot_history = research_mod.fetch_yahoo([cfg.get("price_feed_primary", "XAUUSD=X")],
                                                    range_str="3mo", interval="1d")
            if spot_history:
                summary.setdefault("prices", {}).update({
                    key: {k: v for k, v in value.items() if k != "bars"}
                    for key, value in spot_history.items()
                })
                summary.setdefault("bars", {}).update({
                    key: value.get("bars", []) for key, value in spot_history.items()
                })
        except Exception as exc:
            util.log(f"XAUUSD=X history unavailable; using GC=F: {exc}", "WARN")

    # Alias the closest available daily bars to the book symbol. Confirmed
    # pivots and ATR use completed daily (24h) gold bars, never equity bars.
    bars = summary.setdefault("bars", {})
    prices = summary.setdefault("prices", {})
    spot_symbol = cfg.get("price_feed_primary", "XAUUSD=X")
    gold_history = bars.get(spot_symbol) or bars.get("GC=F") or bars.get("XAUUSD") or []
    if gold_history:
        bars["XAUUSD"] = gold_history
    source_price = prices.get(spot_symbol) or prices.get("GC=F") or prices.get("XAUUSD") or {}
    if source_price:
        prices.setdefault("XAUUSD", dict(source_price))

    # The general research price note maps gold futures to GLD. Add a distinct
    # XAUUSD evidence note so the single-instrument hypothesis remains isolated.
    notes = summary.setdefault("notes", [])
    gold_ref = prices.get(spot_symbol) or prices.get("GC=F") or {}
    change = gold_ref.get("chg_pct")
    if change is not None:
        change = float(change)
        direction = "bullish" if change > 0.004 else "bearish" if change < -0.004 else "neutral"
        _fx_append_research_note(notes, {
            "category": "commodities", "tickers": ["XAUUSD"],
            "title": f"Gold reference moved {change * 100:+.2f}%",
            "summary": f"Gold reference is {change * 100:+.2f}% to "
                       f"${float(gold_ref.get('close') or 0):,.2f}.",
            "direction": direction, "strength": min(0.8, 0.45 + abs(change) * 8),
            "source": gold_ref.get("symbol") or "Yahoo gold reference",
            "date": gold_ref.get("date") or market_fx.fx_day(now).isoformat(),
        })

    # Real yields and broad USD direction are gold-oriented (higher values
    # usually pressure non-yielding gold); raw FRED observations stay in state.
    fred = summary.get("fred") or {}
    real_yield = fred.get("DFII10") or {}
    real_yield_change = real_yield.get("chg_units")
    if real_yield_change is not None and float(real_yield_change) != 0:
        delta = float(real_yield_change)
        _fx_append_research_note(notes, {
            "category": "rates", "tickers": ["XAUUSD"],
            "title": f"10Y real yield {delta:+.3f}pt",
            "summary": "Rising real yields are a gold headwind; falling real yields are a tailwind.",
            "direction": "bearish" if delta > 0 else "bullish",
            "strength": min(0.75, 0.45 + abs(delta) * 4),
            "source": "FRED:DFII10", "date": real_yield.get("date"),
        })
    usd = fred.get("DTWEXBGS") or {}
    usd_change = usd.get("pct_chg")
    if usd_change is not None and float(usd_change) != 0:
        delta = float(usd_change)
        _fx_append_research_note(notes, {
            "category": "fx", "tickers": ["XAUUSD"],
            "title": f"Broad USD {delta * 100:+.2f}%",
            "summary": "A stronger broad USD is a gold headwind; a weaker USD is a tailwind.",
            "direction": "bearish" if delta > 0 else "bullish",
            "strength": min(0.75, 0.45 + abs(delta) * 8),
            "source": "FRED:DTWEXBGS", "date": usd.get("date"),
        })
    return summary


def _fx_hypotheses(st, cfg: dict, summary: dict, now=None) -> tuple[list[dict], dict]:
    from . import indicators, market_fx, signals
    gold_bars = (summary.get("bars") or {}).get("XAUUSD") or []
    tech = {}
    if len(gold_bars) >= 20:
        snapshot = indicators.technical_snapshot(gold_bars)
        if snapshot:
            tech["XAUUSD"] = snapshot
    hyps = signals.build_hypotheses(
        summary, tech, st.data.get("signal_tracker") or {}, ["XAUUSD"],
        {"min_confidence": float(cfg.get("min_confidence", 0.60)), "sector_of": {}},
        today=market_fx.as_et(now).date() if now is not None else None,
    )
    return hyps, tech


def _fx_red_event_guard(summary: dict, cfg: dict, now) -> str | None:
    """Return a reason if a known high-impact event is within the guard window."""
    import re
    from . import market_fx
    local = market_fx.as_et(now)
    buffer_minutes = float(cfg.get("red_event_buffer_minutes", 15))
    red_words = ("nfp", "nonfarm", "cpi", "ppi", "fomc", "ecb", "boe")
    candidates = []
    for event in list((summary or {}).get("events") or []) + list(cfg.get("red_events") or []):
        if isinstance(event, str):
            title, stamp = event, event
        elif isinstance(event, dict):
            title = str(event.get("name") or event.get("title") or "")
            stamp = event.get("at") or event.get("datetime") or ""
            impact = str(event.get("impact") or "red").lower()
            if impact not in {"red", "high", "high-impact"}:
                continue
        else:
            continue
        if not any(word in title.lower() for word in red_words):
            continue
        try:
            when = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
            when = market_fx.as_et(when)
        except (TypeError, ValueError):
            continue
        if when.date() == local.date():
            candidates.append((when, title))

    # Reuse the engine's existing schedule for known NFP/FOMC releases. CPI,
    # PPI, ECB and BoE timestamps can also be supplied in research/config events.
    for label in upcoming_events(local.date()):
        if not any(word in label.lower() for word in red_words):
            continue
        match = re.search(r"(\d{1,2}):(\d{2}) ET", label)
        if not match:
            continue
        when = local.replace(hour=int(match.group(1)), minute=int(match.group(2)),
                             second=0, microsecond=0)
        candidates.append((when, label))
    for when, title in candidates:
        if abs((local - when).total_seconds()) <= buffer_minutes * 60:
            return f"{title} within ±{buffer_minutes:g} min — no new entry"
    return None


def _fx_positions(st, price: float | None = None) -> list[dict]:
    rows = []
    for trade in st.ledger:
        if trade.get("symbol") != "XAUUSD" or trade.get("status") != "open":
            continue
        side = trade.get("side")
        entry = float(trade.get("entry_price") or 0)
        qty = float(trade.get("qty_oz") or trade.get("qty") or 0)
        prior_mark = (st.data.get("last_market_price") or {}).get("price")
        mark = float(price or prior_mark or trade.get("last_mark_price") or entry)
        sign = 1 if side == "long" else -1
        rows.append({
            "symbol": "XAUUSD", "side": side, "qty_oz": qty,
            "entry_price": entry, "stop_price": trade.get("stop_price"),
            "trail_price": trade.get("trail_price"),
            "upnl": (mark - entry) * qty * sign,
            "mark_price": mark,
            "trade": trade,
        })
    return rows


def fx_open_positions(state_dir: str | Path) -> list[dict]:
    """Read-only dispatch helper: current open XAUUSD ledger positions."""
    st, _ = _fx_context(state_dir)
    return [dict(t) for t in st.ledger if t.get("symbol") == "XAUUSD" and
            t.get("status") == "open"]


def fx_had_entry(state_dir: str | Path, fx_day: str) -> bool:
    """Whether this FX day already has an entry, even if it has since exited."""
    st, _ = _fx_context(state_dir)
    return any(t.get("symbol") == "XAUUSD" and t.get("fx_day") == fx_day
               for t in st.ledger)


def _fx_cluster_open_count(cfg: dict) -> int:
    """Read (never mutate) the equity ledger for the shared precious-metals cap."""
    if not cfg.get("cross_book_cluster_check", True):
        return 0
    default_file = Path(__file__).resolve().parent.parent / "state" / "state.json"
    path = Path(cfg.get("equity_state_file") or default_file)
    if not path.exists():
        return 0
    try:
        data = util.read_json(path) or {}
        return sum(1 for trade in data.get("ledger", [])
                   if trade.get("status") == "open" and trade.get("symbol") in {"GLD", "SLV"})
    except Exception as exc:
        util.log(f"equity precious-metals ledger unavailable ({exc}); cluster check skipped", "WARN")
        return 0


def _fx_regrade_stale(st, cfg: dict, hyps: list[dict], label, now) -> list[str]:
    from . import market_fx
    notes = []
    for trade in st.ledger:
        if trade.get("symbol") != "XAUUSD" or trade.get("status") != "open":
            continue
        try:
            opened_day = date.fromisoformat(str(trade.get("fx_day")))
        except (TypeError, ValueError):
            continue
        age = market_fx.fx_trading_days_between(opened_day, label)
        horizon = int(trade.get("thesis_horizon_sessions") or cfg.get("thesis_horizon_sessions", 5))
        if age <= horizon:
            continue
        previous = trade.get("thesis_regrade") or {}
        if previous.get("fx_day") == label.isoformat():
            continue
        current = next((h for h in hyps if h.get("symbol") == "XAUUSD"), None)
        if current:
            assessment = "opposing" if current.get("side") != trade.get("side") else "aligned"
            current_data = {key: current.get(key) for key in
                            ("side", "confidence", "thesis", "falsifiers")}
        else:
            assessment, current_data = "no fresh tradeable hypothesis", None
        trade["stale_thesis"] = True
        trade["thesis_regrade"] = {
            "fx_day": label.isoformat(), "age_sessions": age,
            "assessment": assessment, "current_hypothesis": current_data,
            "at": _fx_iso(now),
        }
        note = (f"XAUUSD thesis exceeded its {horizon}-session horizon ({age} sessions); "
                f"forced re-grade: {assessment}. Stops remain active; no research-only auto-exit.")
        notes.append(note)
        util.log(note, "WARN")
    return notes


def _fx_process_exits(st, cfg: dict, closed: list[dict], summary: dict,
                      now, notify: bool = True, sync_playbook: bool = True) -> list[dict]:
    from . import market_fx
    if not closed:
        return []
    learn = learning.run_learning(closed, st.data.get("signal_tracker") or {},
                                  st.data, cfg, summary)
    st.data["signal_tracker"] = learn["tracker"]
    st.data["lessons"] = learn["lessons"]
    st.data["last_exit_batch"] = {"at": _fx_iso(now), "count": len(closed),
                                  "pnl": round(sum(float(t.get("pnl") or 0) for t in closed), 2),
                                  "fx_day": market_fx.fx_day(now).isoformat()}
    st.record_thinking("XAUUSD exit batch graded per trade: " + "; ".join(
        str(t.get("lesson") or t.get("exit_reason")) for t in closed),
        market_fx.fx_day(now).isoformat())
    if notify:
        try:
            from . import telegram
            for trade in closed:
                telegram.send(telegram.format_exit(trade))
        except Exception as exc:
            util.log(f"FX exit Telegram notification failed: {exc}", "WARN")
    if sync_playbook:
        _fx_sync_playbook(st, cfg, learn["lessons"])
    return closed


def _fx_sync_playbook(st, cfg: dict, lessons: list[str] | None = None) -> None:
    """Upsert only the XAUUSD section, preserving the equity playbook verbatim."""
    path = Path(__file__).resolve().parent.parent / "PLAYBOOK.md"
    begin = "<!-- BEGIN XAUUSD PAPER BOOK -->"
    end = "<!-- END XAUUSD PAPER BOOK -->"
    tracker = st.data.get("signal_tracker") or {}
    lines = [
        begin,
        "## XAUUSD — Swing-with-stops paper book",
        "",
        "Isolated paper-only book. Positions can span FX days; exits are stop, "
        "configured target, explicit thesis-broken action, or the Friday weekend policy.",
        "Risk sizing: 1.0% of book equity divided by entry-to-stop distance; "
        "no pyramiding; 24h structure uses the latest confirmed 3-bar pivot and 3×ATR.",
        "",
    ]
    categories = [c for c in tracker if not str(c).startswith("_") and isinstance(tracker[c], dict)]
    if categories:
        lines.extend(["| Evidence family | n | Win rate | Edge / trade |", "|---|---:|---:|---:|"])
        for category in sorted(categories):
            row = tracker[category]
            win_rate = row.get("win_rate")
            win_label = f"{float(win_rate) * 100:.0f}%" if win_rate is not None else "n/a"
            edge = float(row.get("edge") or 0)
            lines.append(f"| {category} | {row.get('n', 0)} | {win_label} | {edge:+.2f} |")
        lines.append("")
    lines.append("**Recent exit lessons**")
    if lessons:
        lines.extend(f"- {lesson}" for lesson in lessons[:6])
    else:
        lines.append("- No graded XAUUSD exits yet.")
    lines.extend(["", end])
    section = "\n".join(lines)
    try:
        current = path.read_text() if path.exists() else "# A-Trade Playbook\n"
        if begin in current and end in current:
            before = current.split(begin, 1)[0].rstrip()
            after = current.split(end, 1)[1].lstrip("\n")
            updated = before + "\n\n" + section + ("\n\n" + after if after else "\n")
        else:
            updated = current.rstrip() + "\n\n" + section + "\n"
        if updated != current:
            path.write_text(updated)
    except Exception as exc:
        util.log(f"XAUUSD playbook section update failed: {exc}", "WARN")


def _fx_mark_and_drawdown(st, broker, cfg: dict, now, realized: bool = False,
                          price: float | None = None) -> tuple[float, str | None]:
    from . import market_fx
    try:
        equity = float(broker.account().get("equity") or cfg.get("initial_equity", 100000.0))
    except Exception:
        equity = float(cfg.get("initial_equity", 100000.0)) + sum(
            float(t.get("pnl") or 0) for t in st.ledger)
    label = market_fx.fx_day(now).isoformat()
    open_positions = _fx_positions(st, price)
    unrealized = sum(float(row["upnl"]) for row in open_positions)
    last_mark = st.data.get("last_fx_mark_day")
    if last_mark != label or realized:
        st.record_score({
            "at": _fx_iso(now), "fx_day": label, "equity": round(equity, 2),
            "unrealized_pnl": round(unrealized, 2), "mark": True,
            "realized": bool(realized),
        })
        st.data["last_fx_mark_day"] = label
    peak = float(st.data.get("peak_equity") or cfg.get("initial_equity", 100000.0))
    peak = max(peak, equity)
    st.data["peak_equity"] = round(peak, 2)
    limit = float(cfg.get("max_drawdown_pct", 0.14))
    if limit > 0 and peak > 0 and equity < peak * (1.0 - limit) and not st.paused:
        reason = (f"XAUUSD max drawdown exceeded: equity {equity:,.2f} vs peak {peak:,.2f} "
                  f"({(1 - equity / peak) * 100:.1f}% below peak; limit {limit * 100:.1f}%)")
        st.data["resume"] = {"paused": True, "reason": reason, "paused_at": _fx_iso(now)}
        util.log(reason, "ERROR")
        return equity, reason
    return equity, None


def _fx_pause_notice(reason: str, equity: float, notify: bool = True) -> None:
    if not notify:
        return
    try:
        from . import telegram
        formatter = getattr(telegram, "format_fx_paused", telegram.format_paused)
        telegram.send(formatter(reason, f"XAUUSD paper equity {equity:,.2f}"))
    except Exception as exc:
        util.log(f"FX pause Telegram notification failed: {exc}", "WARN")


def fx_tick(state_dir: str | Path, now=None, price=None, *, notify: bool = True,
            sync_playbook: bool = True) -> dict:
    """Tick-level stop pass. Dispatcher calls this before every window check.

    When flat it is a cheap no-op. With a position, it requires a fresh XAUUSD=X
    or GC=F quote and compares the market against stop/trail/target on every tick.
    """
    import json
    from . import market_fx
    st, cfg = _fx_context(state_dir)
    local = market_fx.as_et(now)
    label = market_fx.fx_day(local).isoformat()
    session = market_fx.fx_session(local)
    if not market_fx.is_fx_open(local):
        if sync_playbook:
            _fx_sync_playbook(st, cfg, st.data.get("lessons") or [])
        return {"status": "closed", "session": session, "fx_day": label,
                "price": None, "closed": []}

    open_positions = [t for t in st.ledger if t.get("symbol") == "XAUUSD" and
                      t.get("status") == "open"]
    if not open_positions:
        # No quote is needed for stop protection while flat. A scheduled open
        # run obtains its own fresh quote after this idempotent tick pass.
        if sync_playbook:
            _fx_sync_playbook(st, cfg, st.data.get("lessons") or [])
        return {"status": "flat", "session": session, "fx_day": label,
                "price": None, "closed": []}

    quote = _fx_quote(price, now=local) if price is not None else _fetch_fx_quote(cfg, local)
    if quote is None:
        changed = False
        if st.data.get("last_fx_quote_alert_day") != label:
            st.data["last_fx_quote_alert_day"] = label
            changed = True
            if notify:
                try:
                    from . import telegram
                    telegram.send(telegram.format_error(
                        "XAUUSD stop check", "No fresh XAUUSD=X or GC=F quote; "
                        "the tick check ran but could not compare/execute stops."))
                except Exception:
                    pass
        if changed:
            st.save()
        if sync_playbook:
            _fx_sync_playbook(st, cfg, st.data.get("lessons") or [])
        return {"status": "price_unavailable", "session": session,
                "fx_day": label, "price": None, "closed": []}

    before = json.dumps(st.data, sort_keys=True, default=str)
    new_fx_day_mark = st.data.get("last_fx_mark_day") != label
    old_extrema = {t.get("trade_id"): t.get("entry_extremum") for t in open_positions}
    paper = _fx_broker(st, cfg, quote, "tick_stop", drift_scale=0.0)
    current = float(quote["price"])
    prices = {"XAUUSD": current}
    trading.update_entry_extrema(st.ledger, prices)
    extremum_changed = any(old_extrema.get(t.get("trade_id")) != t.get("entry_extremum")
                           for t in open_positions)
    closed = trading.check_stops(paper, cfg, st.ledger, prices, now=local)
    _fx_process_exits(st, cfg, closed, {}, local, notify, sync_playbook)
    equity, pause_reason = _fx_mark_and_drawdown(st, paper, cfg, local,
                                                 realized=bool(closed), price=current)
    if pause_reason:
        _fx_pause_notice(pause_reason, equity, notify)
    # Persist the quote at most once per FX day, on an exit, or when a new
    # chandelier extreme must survive a later scheduled risk-check.
    if new_fx_day_mark or closed or extremum_changed:
        st.data["last_market_price"] = {"symbol": quote.get("symbol"), "price": current,
                                        "at": quote.get("at"), "fx_day": label}
    after = json.dumps(st.data, sort_keys=True, default=str)
    if after != before:
        st.save()
    if sync_playbook:
        _fx_sync_playbook(st, cfg, st.data.get("lessons") or [])
    return {"status": "ok", "session": session, "fx_day": label,
            "price": current, "source": quote.get("symbol"), "at": quote.get("at"),
            "closed": closed, "equity": round(equity, 2)}


def swing_open_run(state_dir: str | Path, now=None, price=None, summary: dict | None = None,
                   *, notify: bool = True, sync_playbook: bool = True) -> dict:
    """Research and open at most one non-pyramiding XAUUSD swing position."""
    from . import market_fx
    st, cfg = _fx_context(state_dir)
    local = market_fx.as_et(now)
    label = market_fx.fx_day(local)
    if st.paused:
        return {"status": "paused", "opened": [], "skipped": ["FX book is auto-paused"]}
    if not market_fx.is_fx_open(local):
        return {"status": "closed", "opened": [], "skipped": [f"FX market {market_fx.fx_session(local)}"]}

    quote = _fx_quote(price, now=local) if price is not None else _fetch_fx_quote(cfg, local)
    if quote is None:
        return {"status": "price_unavailable", "opened": [], "skipped": ["no fresh gold quote"]}
    if any(t.get("symbol") == "XAUUSD" and t.get("fx_day") == label.isoformat()
           for t in st.ledger):
        return {"status": "already_entered", "opened": [], "skipped": [
            f"one-entry-per-FX-day guard ({label.isoformat()})"]}

    summary = _fx_research(st, cfg, local, summary=summary)
    hyps, _tech = _fx_hypotheses(st, cfg, summary, local)
    tradeable = [h for h in hyps if h.get("tradeable") and
                 float(h.get("confidence") or 0) >= float(cfg.get("min_confidence", 0.60))]
    if not tradeable:
        return {"status": "no_signal", "opened": [], "skipped": [
            "no XAUUSD hypothesis met the confidence threshold"], "hypotheses": hyps}
    event_block = _fx_red_event_guard(summary, cfg, local)
    if event_block:
        return {"status": "event_guard", "opened": [], "skipped": [event_block],
                "hypotheses": hyps}

    paper = _fx_broker(st, cfg, quote, "swing_open")
    open_trades = [t for t in st.ledger if t.get("symbol") == "XAUUSD" and
                   t.get("status") == "open"]
    broker_positions = paper.positions()
    broker_open = [p for p in broker_positions if p.get("symbol") == "XAUUSD" and
                   abs(float(p.get("qty") or 0)) > 0.0005]
    if open_trades or broker_open:
        return {"status": "no_pyramiding", "opened": [], "skipped": [
            "an XAUUSD position already exists; pyramiding is disabled"], "hypotheses": hyps}
    cluster_open = _fx_cluster_open_count(cfg)
    cluster_cap = int(cfg.get("max_per_cluster", 2))
    if cluster_open >= cluster_cap:
        return {"status": "cluster_cap", "opened": [], "skipped": [
            f"precious_metals cluster already has {cluster_open}/{cluster_cap} positions"],
            "hypotheses": hyps}

    bars = (summary.get("bars") or {}).get("XAUUSD") or []
    # Ignore a same-day unfinished daily candle; the pivot/ATR inputs are
    # completed 24h reference bars only.
    completed_bars = _fx_completed_bars(bars, local)
    atr_value = trading.atr_24h(completed_bars, int(cfg.get("atr_period", 14)))
    if not atr_value:
        return {"status": "no_atr", "opened": [], "skipped": [
            "no usable completed 24h gold bars for initial stop"], "hypotheses": hyps}

    hypothesis = tradeable[0]
    side = hypothesis["side"]
    market_price = float(paper.get_price("XAUUSD") or quote["price"])
    slip = market_price * float(cfg.get("slippage_bps", 2.0)) / 10000.0
    half_spread = float(cfg.get("spread", 0.30)) / 2.0
    expected_entry = market_price + half_spread + slip if side == "long" else market_price - half_spread - slip
    stop = trading.swing_initial_stop(
        expected_entry, side, completed_bars, atr_value,
        atr_mult=float(cfg.get("initial_stop_atr_mult", 3.0)), pivot_bars=3)
    if not stop:
        return {"status": "no_stop", "opened": [], "skipped": [
            "could not derive a valid confirmed-pivot/3x-ATR stop"], "hypotheses": hyps}

    try:
        equity = float(paper.account().get("equity") or cfg.get("initial_equity", 100000.0))
    except Exception:
        equity = float(cfg.get("initial_equity", 100000.0))
    qty = trading.swing_size(equity, float(cfg.get("risk_pct", 0.01)),
                             expected_entry, stop["stop_price"], precision=3)
    if qty <= 0:
        return {"status": "size_too_small", "opened": [], "skipped": [
            "risk formula rounded position below 0.001 troy oz"], "hypotheses": hyps}
    # Cross-book cap includes any open XAUUSD position, GLD, and SLV. Existing
    # equity state is inspected read-only; no equity engine code is invoked.
    if cluster_open + 1 > cluster_cap:
        return {"status": "cluster_cap", "opened": [], "skipped": [
            f"entry would exceed precious_metals cap ({cluster_open + 1}/{cluster_cap})"],
            "hypotheses": hyps}
    order_side = "buy" if side == "long" else "sell"
    try:
        order = paper.submit_order("XAUUSD", qty, order_side)
    except Exception as exc:
        util.log(f"XAUUSD swing order failed: {exc}", "ERROR")
        return {"status": "order_error", "opened": [], "skipped": [str(exc)],
                "hypotheses": hyps}
    entry = float(order.get("filled_avg_price") or expected_entry)
    # Re-anchor the stop to the actual fill (the paper mock's deterministic
    # quote means this should be equal to expected_entry up to price rounding).
    stop = trading.swing_initial_stop(
        entry, side, completed_bars, atr_value,
        atr_mult=float(cfg.get("initial_stop_atr_mult", 3.0)), pivot_bars=3) or stop
    actual_risk = abs(entry - float(stop["stop_price"]))
    # The filled amount is fixed by the order; record its exact realized risk.
    take_profit = None
    if cfg.get("take_profit_r") not in (None, "", 0, 0.0):
        reward_r = float(cfg["take_profit_r"])
        take_profit = round(entry + actual_risk * reward_r if side == "long"
                            else entry - actual_risk * reward_r, 2)
    trade_id = f"XAUUSD-{label.isoformat()}-{local.strftime('%H%M%S')}"
    trade = {
        "trade_id": trade_id, "symbol": "XAUUSD", "side": side,
        "qty": float(order.get("qty") or qty), "qty_oz": float(order.get("qty") or qty),
        "entry_price": round(entry, 4), "order_id": order.get("id"),
        "stop_price": float(stop["stop_price"]), "trail_price": None,
        "trail_atr_mult": float(cfg.get("trail_atr_mult", 3.0)),
        "take_profit": take_profit,
        "thesis_horizon": hypothesis.get("thesis_horizon") or cfg.get("thesis_horizon", "2-5 sessions"),
        "thesis_horizon_sessions": int(cfg.get("thesis_horizon_sessions", 5)),
        "entry_extremum": round(entry, 4), "initial_risk_per_oz": round(actual_risk, 4),
        "risk_usd": round(float(order.get("qty") or qty) * actual_risk, 2),
        "stop_basis": stop.get("stop_basis"), "stop_structure_price": stop.get("structure_price"),
        "atr_24h": stop.get("atr"), "fx_day": label.isoformat(),
        "opened_at": _fx_iso(local),
        "hypothesis": {key: hypothesis.get(key) for key in
                       ("thesis", "falsifiers", "confidence", "dominant_category", "evidence")},
        "hypothesis_id": trade_id, "status": "open", "pnl": None,
        "pnl_pct": None, "exit_price": None, "closed_at": None,
        "exit_reason": None, "stop_hit": False, "notes": [],
    }
    st.add_trades([trade])
    st.data["last_market_price"] = {"symbol": quote.get("symbol"),
                                    "price": float(quote["price"]),
                                    "at": quote.get("at"), "fx_day": label.isoformat()}
    marked_equity, pause_reason = _fx_mark_and_drawdown(
        st, paper, cfg, local, price=float(quote["price"]))
    if pause_reason:
        _fx_pause_notice(pause_reason, marked_equity, notify)
    st.data["last_open_fx"] = {"at": _fx_iso(local), "fx_day": label.isoformat(),
                               "opened": trade_id, "source": quote.get("symbol"),
                               "confidence": hypothesis.get("confidence")}
    st.record_run({"type": "swing_open", "at": _fx_iso(local), "fx_day": label.isoformat(),
                   "opened": 1, "source": quote.get("symbol")})
    st.save()
    if notify:
        try:
            from . import telegram
            telegram.send(telegram.format_swing_open(
                _fx_iso(local), [trade], [], fx_day=label.isoformat(),
                session=market_fx.fx_session(local), atr=stop.get("atr")))
        except Exception as exc:
            util.log(f"XAUUSD swing-open Telegram notification failed: {exc}", "WARN")
    if sync_playbook:
        _fx_sync_playbook(st, cfg, st.data.get("lessons") or [])
    util.log(f"GOLD SWING OPEN {side.upper()} {trade['qty_oz']:.3f} oz @ {entry:.2f}; "
             f"stop {trade['stop_price']:.2f}; risk ${trade['risk_usd']:.2f}")
    return {"status": "ok", "opened": [trade], "skipped": [], "hypotheses": hyps,
            "mode": "mock_paper", "quote": quote, "atr": stop.get("atr")}


def _fx_stale_date(now, horizon: int) -> date:
    from . import market_fx
    label = market_fx.fx_day(now)
    cursor = label
    count = 0
    while count < max(1, int(horizon)):
        cursor -= timedelta(days=1)
        if market_fx.is_fx_trading_day(cursor):
            count += 1
    return cursor


def risk_check_run(state_dir: str | Path, now=None, price=None, summary: dict | None = None,
                   *, block: str = "checkin_am", notify: bool = True,
                   sync_playbook: bool = True) -> dict:
    """Manage open swing positions; never daily-flattens (Friday policy only)."""
    from . import market_fx
    st, cfg = _fx_context(state_dir)
    local = market_fx.as_et(now)
    label = market_fx.fx_day(local)
    quote = _fx_quote(price, now=local) if price is not None else _fetch_fx_quote(cfg, local)
    summary = _fx_research(st, cfg, local, summary=summary)
    hyps, _tech = _fx_hypotheses(st, cfg, summary, local)
    current = float(quote["price"]) if quote else None
    paper = _fx_broker(st, cfg, quote, f"risk_check_{block}", drift_scale=0.0)
    updates = []
    closed = []
    price_map = {"XAUUSD": current} if current is not None else {}
    atr_value = None
    if current is not None:
        trading.update_entry_extrema(st.ledger, price_map)
        bars = (summary.get("bars") or {}).get("XAUUSD") or []
        completed_bars = _fx_completed_bars(bars, local)
        atr_value = trading.atr_24h(completed_bars, int(cfg.get("atr_period", 14)))
        updates = trading.trail_stops(st.ledger, price_map, atr_value, cfg)
        closed.extend(trading.check_stops(paper, cfg, st.ledger, price_map, now=local))

    friday_preclose = (local.weekday() == 4 and
                       market_fx.fx_session(local) not in {"weekend", "break", "holiday"})
    weekend_flat = friday_preclose and bool(cfg.get("flatten_before_weekend", True))
    if weekend_flat and current is not None:
        closed.extend(trading.close_swing_positions(paper, st.ledger, price_map,
                                                    "weekend_flat", now=local))
    elif friday_preclose and current is not None and cfg.get("weekend_mode") == "hold_with_tightened_stops":
        # Explicit opt-in alternative: tighten to breakeven, never loosen an
        # existing stop. The default remains flat-before-weekend.
        for trade in st.ledger:
            if trade.get("symbol") != "XAUUSD" or trade.get("status") != "open":
                continue
            entry = float(trade.get("entry_price") or current)
            side = trade.get("side")
            old = trade.get("trail_price")
            old_value = float(old) if old is not None else entry
            new_value = max(old_value, entry) if side == "long" else min(old_value, entry)
            if old is None or abs(new_value - old_value) >= 0.01:
                trade["trail_price"] = round(new_value, 2)
                updates.append({"symbol": "XAUUSD", "side": side, "old": old,
                                "new": round(new_value, 2), "reason": "weekend breakeven tighten"})
        closed.extend(trading.check_stops(paper, cfg, st.ledger, price_map, now=local))

    exit_ids = {trade.get("trade_id") for trade in closed}
    # Avoid grading twice if a stop and a subsequent policy check found the
    # same ledger item in the same pass.
    closed = list({trade.get("trade_id"): trade for trade in closed}.values())
    _fx_process_exits(st, cfg, closed, summary, local, notify, sync_playbook)
    stale_notes = _fx_regrade_stale(st, cfg, hyps, label, local)
    equity, pause_reason = _fx_mark_and_drawdown(st, paper, cfg, local,
                                                 realized=bool(closed), price=current)
    if pause_reason:
        _fx_pause_notice(pause_reason, equity, notify)

    rows = _fx_positions(st, current)
    events = upcoming_events(local.date())
    if quote:
        st.data["last_market_price"] = {"symbol": quote.get("symbol"),
                                        "price": current, "at": quote.get("at"),
                                        "fx_day": label.isoformat()}
    st.data["last_risk_check_fx"] = {"at": _fx_iso(local), "fx_day": label.isoformat(),
                                    "block": block, "price": current,
                                    "quote_source": quote.get("symbol") if quote else None,
                                    "updates": updates, "closed": [t.get("trade_id") for t in closed],
                                    "stale_theses": stale_notes}
    st.record_run({"type": "risk_check", "at": _fx_iso(local), "fx_day": label.isoformat(),
                   "block": block, "open": len(rows), "closed": len(closed)})
    st.save()
    if notify:
        try:
            from . import telegram
            telegram.send(telegram.format_risk_check(
                _fx_iso(local), rows, updates, events, fx_day=label.isoformat(),
                block=block, quote_available=current is not None,
                stale_notes=stale_notes, paused=st.paused))
        except Exception as exc:
            util.log(f"XAUUSD risk-check Telegram notification failed: {exc}", "WARN")
    if sync_playbook:
        _fx_sync_playbook(st, cfg, st.data.get("lessons") or [])
    return {"status": "ok", "block": block, "positions": rows, "updates": updates,
            "closed": closed, "stale_theses": stale_notes, "price": current,
            "equity": round(equity, 2), "realized": bool(closed),
            "exit_ids": sorted(str(i) for i in exit_ids if i)}


def session_preview_run(state_dir: str | Path, now=None, summary: dict | None = None,
                        *, notify: bool = True) -> dict:
    """Send the next FX-session plan; informational only, no orders."""
    from . import market_fx
    st, cfg = _fx_context(state_dir)
    local = market_fx.as_et(now)
    summary = _fx_research(st, cfg, local, summary=summary)
    hyps, _tech = _fx_hypotheses(st, cfg, summary, local)
    events = []
    for offset in range(1, 4):
        day = local.date() + timedelta(days=offset)
        if market_fx.is_fx_trading_day(day):
            events.extend(f"{day.strftime('%a %b %d')} — {item}"
                          for item in upcoming_events(day)
                          if "No major" not in item)
    if not events:
        events = ["No scheduled high-impact events flagged in the existing calendar."]
    next_session = market_fx.fx_session(local + timedelta(hours=1))
    rows = _fx_positions(st)
    try:
        from . import telegram
        if notify:
            telegram.send(telegram.format_session_preview(
                _fx_iso(local), next_session, events, hyps, rows,
                paused=st.paused, price=(st.data.get("last_market_price") or {}).get("price")))
    except Exception as exc:
        util.log(f"XAUUSD session-preview Telegram notification failed: {exc}", "WARN")
    st.record_run({"type": "session_preview", "at": _fx_iso(local),
                   "fx_day": market_fx.fx_day(local).isoformat(), "hypotheses": len(hyps)})
    st.save()
    return {"status": "ok", "hypotheses": hyps, "events": events,
            "next_session": next_session}


def fx_week_ahead_run(state_dir: str | Path, now=None, summary: dict | None = None,
                      *, notify: bool = True) -> dict:
    """Sunday informational digest with FX events and the DXY/real-yield backdrop."""
    from . import market_fx
    st, cfg = _fx_context(state_dir)
    local = market_fx.as_et(now)
    summary = _fx_research(st, cfg, local, summary=summary)
    hyps, _tech = _fx_hypotheses(st, cfg, summary, local)
    days_ahead = (0 - local.date().weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    monday = local.date() + timedelta(days=days_ahead)
    week_days = [monday + timedelta(days=i) for i in range(5)]
    events = upcoming_week_events(week_days)
    fred = summary.get("fred") or {}
    try:
        from . import telegram
        if notify:
            telegram.send(telegram.format_fx_week_ahead(
                _fx_iso(local), week_days, events, hyps, fred,
                paused=st.paused, open_positions=_fx_positions(st)))
    except Exception as exc:
        util.log(f"XAUUSD week-ahead Telegram notification failed: {exc}", "WARN")
    st.record_run({"type": "week_ahead", "at": _fx_iso(local),
                   "fx_day": market_fx.fx_day(local).isoformat(),
                   "week_start": monday.isoformat(), "hypotheses": len(hyps)})
    st.save()
    return {"status": "ok", "week_start": monday.isoformat(),
            "events": events, "hypotheses": hyps}



def thesis_broken_exit(state_dir: str | Path, trade_id: str, now=None, price=None,
                       *, notify: bool = True, sync_playbook: bool = True) -> dict:
    """Explicitly close the sole XAUUSD swing on an operator-confirmed thesis break.

    The v1 policy deliberately does not infer a thesis break from prose or an
    opposite research hypothesis. This function is an explicit control hook.
    """
    from . import market_fx
    st, cfg = _fx_context(state_dir)
    local = market_fx.as_et(now)
    quote = _fx_quote(price, now=local) if price is not None else _fetch_fx_quote(cfg, local)
    if not quote:
        return {"status": "price_unavailable", "closed": []}
    selected = [t for t in st.ledger if t.get("trade_id") == trade_id and
                t.get("symbol") == "XAUUSD" and t.get("status") == "open"]
    if not selected:
        return {"status": "not_open", "closed": []}
    paper = _fx_broker(st, cfg, quote, "thesis_broken", drift_scale=0.0)
    prices = {"XAUUSD": float(quote["price"])}
    # The no-pyramiding default means there is at most one FX position. Refuse
    # the manual close if state contains multiple records rather than over-close.
    if len(selected) != 1:
        return {"status": "position_mismatch", "closed": []}
    trade = selected[0]
    closed = trading.close_swing_positions(paper, [trade], prices,
                                           "thesis_broken", now=local)
    _fx_process_exits(st, cfg, closed, {}, local, notify, sync_playbook)
    st.save()
    return {"status": "ok" if closed else "close_failed", "closed": closed}
