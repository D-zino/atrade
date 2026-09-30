"""Optional Telegram notifications via the Bot API.

To enable, add to atrade/.env:
    TELEGRAM_BOT_TOKEN=123456:ABC...     (from @BotFather)
    TELEGRAM_CHAT_ID=123456789           (your chat/user id; see README)

On GitHub Actions the same two values must be repo Secrets — a missing
secret used to look like a successful run (the send was a silent no-op).

Without credentials every call degrades gracefully to a no-op — the agent
never fails because Telegram is unreachable.
"""
from __future__ import annotations

import html as html_lib
import urllib.error
import urllib.parse
import urllib.request

from . import config as config_mod, market, util

# Last send() outcome — dispatch uses this so GitHub Action *test* steps
# fail loudly instead of going green when nothing was delivered.
last_ok: bool | None = None
last_error: str | None = None


def _keys() -> dict:
    return config_mod.load_env_keys()


def configured() -> bool:
    k = _keys()
    return bool(k.get("TELEGRAM_BOT_TOKEN") and k.get("TELEGRAM_CHAT_ID"))


def _escape(s) -> str:
    """Escape text so Telegram's strict HTML parser doesn't reject the message.

    Unescaped '&' in everyday strings like 'P&L' or 'S&P 500' is a 400 from
    the Bot API ('can't parse entities') — the #1 reason Action alerts vanish.
    """
    return html_lib.escape("" if s is None else str(s), quote=False)


def _as_int(v, default: int = 0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _as_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _post(token: str, chat: str, text: str, parse_mode: str) -> tuple[bool, str | None]:
    payload = {
        "chat_id": chat,
        "text": text,
        "disable_web_page_preview": "true",
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode
    data = urllib.parse.urlencode(payload).encode()
    try:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=20) as resp:
            resp.read()
        return True, None
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        err = f"HTTP {e.code}: {detail or e}"
        return False, err
    except Exception as e:
        return False, str(e)


def send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message; returns True if delivered. No-op when not configured."""
    global last_ok, last_error
    last_ok, last_error = False, None
    k = _keys()
    token = (k.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (k.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat:
        last_error = "Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)"
        util.log(last_error + " — skipping notification", "WARN")
        return False
    ok = True
    for chunk in (text[i:i + 3950] for i in range(0, len(text), 3950)):  # API limit 4096
        delivered, err = _post(token, chat, chunk, parse_mode)
        if delivered:
            continue
        # HTML parse failures (unescaped &, <, >) are common — retry as plain text
        # so a single bad title never silently drops the whole alert.
        if parse_mode:
            util.log(f"telegram HTML send failed ({err}) — retrying as plain text", "WARN")
            delivered, err = _post(token, chat, chunk, parse_mode="")
        if not delivered:
            last_error = err or "telegram send failed"
            util.log(f"telegram send failed: {last_error}", "WARN")
            ok = False
    last_ok = ok
    return ok


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
def _falsifier(h: dict) -> str:
    fals = h.get("falsifiers") or []
    return _escape(fals[0] if fals else "—")


def format_open(asof: str, mode: str, universe_size: int, opened: list,
                skipped: list, hyps: list, adds: list | None = None) -> str:
    lines = [
        "<b>🟢 A-TRADE — OPEN RUN</b>",
        f"<i>{_escape(asof)} · {_escape(mode)} mode</i>",
        "",
    ]
    if opened:
        lines.append("<b>Opened today:</b>")
        for t in opened:
            h = t.get("hypothesis") or {}
            lines.append(f"  • {_escape(t['symbol'])} <b>{_escape(str(t['side']).upper())}</b> "
                         f"{t['qty']} sh "
                         f"@ ${t.get('entry_price', 0):,.2f} (conf {h.get('confidence', 0) * 100:.0f}%)")
    else:
        lines.append("<b>Opened today:</b> none — no hypothesis ≥ 60% confidence")
    if skipped:
        lines.append("<i>   skipped: "
                     + _escape("; ".join(str(s)[:70] for s in skipped[:3]))
                     + "</i>")
    lines.append("")
    lines.append(f"<b>Scanning {universe_size} symbols</b> + FRED macro (yields/CPI/PPI/jobs) "
                 "+ SEC filings (insider Form 4s) + news + FX")
    if adds:
        lines.append(f"<i>dynamic adds today: {_escape(', '.join(str(a) for a in adds[:8]))}</i>")
    lines.append("")
    lines.append("<b>Active hypotheses / watchlist (ranked by confidence):</b>")
    for h in hyps[:8]:
        lines.append(f"  • {_escape(h['symbol'])} <b>{_escape(str(h['side']).upper())}</b> — conf "
                     f"{h['confidence'] * 100:.0f}% · {_escape(h.get('dominant_category', '?'))}")
    lines.append("")
    lines.append("<i>Falsifies if: " + _falsifier((hyps[0] if hyps else {})) + "</i>" if hyps else "")
    return "\n".join(lines)


def format_close(asof: str, mode: str, closed: list, score, improved: bool,
                 net_pnl: float, lessons: list, tracker: dict, hyps: list) -> str:
    lines = [
        "<b>🔴 A-TRADE — CLOSE RUN</b>",
        f"<i>{_escape(asof)} · {_escape(mode)} mode</i>",
        "",
    ]
    if closed:
        lines.append("<b>Day trades closed:</b>")
        for t in closed:
            mark = "✅" if t.get("hypothesis_correct") else "❌"
            lines.append(f"  • {_escape(t['symbol'])} <b>{_escape(str(t['side']).upper())}</b> "
                         f"{t.get('pnl', 0):+,.2f} ({t.get('pnl_pct', 0) * 100:+.2f}%) {mark}")
    else:
        lines.append("<b>Day trades closed:</b> none")
    lines.append("")
    lines.append(f"<b>Composite score:</b> {score:.3f} "
                 f"{'🎉 improved (new best)' if improved else '— no improvement'}")
    lines.append(f"<b>Realized P&amp;L:</b> {net_pnl:+,.2f}")
    if lessons:
        lines.append("")
        lines.append("<b>Lessons learned:</b>")
        for l in lessons[:4]:
            lines.append(f"  • {_escape(str(l)[:150])}")
    cats = sorted([c for c in tracker if not c.startswith("_")],
                  key=lambda c: -(tracker[c].get("win_rate") or 0))[:5]
    if cats:
        lines.append("")
        lines.append("<b>Signal tracker (learned edge):</b>")
        for c in cats:
            st = tracker[c]
            wr = f"{st['win_rate'] * 100:.0f}%" if st.get("win_rate") is not None else "n/a"
            lines.append(f"  • {_escape(c)}: n={st.get('n', 0)} win={wr}")
    lines.append("")
    lines.append("<b>Watchlist for tomorrow / next session:</b>")
    for h in hyps[:6]:
        lines.append(f"  • {_escape(h['symbol'])} <b>{_escape(str(h['side']).upper())}</b> — conf "
                     f"{h['confidence'] * 100:.0f}% · {_escape(h.get('dominant_category', '?'))}")
    lines.append("")
    lines.append("<i>See PLAYBOOK.md for the full updated playbook.</i>")
    return "\n".join(lines)


def format_checkin(asof: str, mode: str, positions: list, event_notes: list) -> str:
    """Mid-session check-in: open positions with unrealized P&L + events in play."""
    lines = [
        "<b>🟡 A-TRADE — MID-SESSION CHECK-IN</b>",
        f"<i>{_escape(asof)} · {_escape(mode)} mode</i>",
        "",
    ]
    if positions:
        lines.append("<b>Open positions:</b>")
        for p in positions:
            if not isinstance(p, dict):
                continue
            sym = _escape(p.get("symbol"))
            qty = _as_int(p.get("qty") or 0)
            upnl = _as_float(p.get("unrealized_pl") or 0)
            side = "LONG" if qty > 0 else "SHORT"
            arrow = "🟢" if upnl >= 0 else "🔴"
            lines.append(f"  • {sym} <b>{side}</b> {abs(qty)} sh — {upnl:+,.2f} {arrow}")
    else:
        lines.append("<b>Open positions:</b> none (all flat)")
    lines.append("")
    if event_notes:
        lines.append("<b>Events in play:</b>")
        for n in event_notes[:4]:
            title = n.get("title") if isinstance(n, dict) else n
            lines.append(f"  • {_escape(title)}")
    else:
        lines.append("<b>Events in play:</b> none flagged")
    lines.append("")
    lines.append("<i>Day-trade policy: positions are held to the close run. No action needed now.</i>")
    return "\n".join(lines)


def format_preview(asof: str, next_day: str, events: list, hyps: list) -> str:
    """Tomorrow preview: next trading day, calendar, prior-adjusted watchlist."""
    lines = [
        "<b>🌙 A-TRADE — TOMORROW PREVIEW</b>",
        f"<i>{_escape(asof)}</i>",
        "",
        f"<b>Next trading day:</b> {_escape(next_day)}",
        "",
        "<b>On the calendar:</b>",
    ]
    for e in events:
        lines.append(f"  • {_escape(e)}")
    lines.append("")
    lines.append("<b>Watchlist with confidence (prior-adjusted from today's grading):</b>")
    if hyps:
        for h in hyps[:8]:
            lines.append(f"  • {_escape(h['symbol'])} <b>{_escape(str(h['side']).upper())}</b> — "
                         f"{h['confidence'] * 100:.0f}% ({_escape(h.get('dominant_category', '?'))})")
    else:
        lines.append("  • none — no signal-rich setups tonight")
    lines.append("")
    lines.append("<b>Theses would change if:</b>")
    for h in hyps[:4]:
        fals = (h.get("falsifiers") or ["new contradictory information"])
        lines.append(f"  • {_escape(h['symbol'])}: {_escape(fals[0])}")
    lines.append("")
    lines.append("<i>These are experiments — every thesis lists its falsifiers in the full reports.</i>")
    return "\n".join(lines)


def format_week_ahead(asof: str, week_days: list, events: list, hyps: list,
                      fred: dict | None = None, paused_note: str | None = None) -> str:
    """Sunday 17:00 ET — week-ahead digest: macro backdrop, calendar, watchlist."""
    fred = fred or {}
    monday, friday = week_days[0], week_days[-1]
    lines = [
        "<b>📅 A-TRADE — WEEK-AHEAD PREVIEW</b>",
        f"<i>{_escape(asof)}</i>",
        "",
        f"<b>Next trading week:</b> {_escape(market.date_str(monday))} → {_escape(market.date_str(friday))}",
        "",
        "<b>Macro backdrop:</b>",
    ]
    d10, d2, ff = fred.get("DGS10"), fred.get("DGS2"), fred.get("FEDFUNDS")
    added = 0
    if d10 and d10.get("value") is not None:
        lines.append(f"  • 10Y yield {d10['value']:.2f}%")
        added += 1
    if d2 and d2.get("value") is not None:
        lines.append(f"  • 2Y yield {d2['value']:.2f}%")
        added += 1
    if ff and ff.get("value") is not None:
        lines.append(f"  • Fed funds {ff['value']:.2f}%")
        added += 1
    if added == 0:
        lines.append("  • (FRED data unavailable this pass)")
    lines.append("")
    lines.append("<b>Calendar highlights:</b>")
    for e in events[:9]:
        lines.append(f"  • {_escape(e)}")
    lines.append("")
    lines.append("<b>Week watchlist (conf = prior-adjusted):</b>")
    for h in hyps[:8]:
        lines.append(f"  • {_escape(h['symbol'])} <b>{_escape(str(h['side']).upper())}</b> — "
                     f"{h['confidence'] * 100:.0f}% ({_escape(h.get('dominant_category', '?'))})")
    if not hyps:
        lines.append("  • none flagged this week")
    lines.append("")
    lines.append("<b>Biggest falsifier risks this week:</b>")
    for h in hyps[:3]:
        fals = (h.get("falsifiers") or ["new contradictory information"])[0]
        lines.append(f"  • {_escape(h['symbol'])}: {_escape(fals)}")
    lines.append("")
    if paused_note:
        lines.append(_escape(paused_note))
        lines.append("")
    lines.append("<i>Weekly setup; the daily 09:25 / 10:30 / 15:50 / 20:00 ET "
                 "messages refine it day by day.</i>")
    return "\n".join(lines)


def format_paused(reason: str, stats: str) -> str:
    return (f"<b>⏸ A-TRADE — AUTO-PAUSED</b>\n\n{_escape(reason)}\n\n{_escape(stats)}\n\n"
            f"Trading is halted. Review, then resume with: python3 -m atrade.cli resume")


def format_error(run_type: str, err: str) -> str:
    return (f"<b>⚠️ A-TRADE — {_escape(run_type.upper())} RUN ERROR</b>\n\n"
            f"<code>{_escape(str(err)[:1000])}</code>")


def format_test() -> str:
    """Setup-verification ping used by the GitHub Action test_telegram input."""
    return (
        "<b>✅ A-Trade is LIVE on GitHub Actions</b>\n\n"
        "Your Telegram alerts are working. You will now receive these messages "
        "every trading day:\n"
        "  • 09:25 ET — open run (what it opened + today's watchlist)\n"
        "  • 10:30 ET — mid-session check-in (positions + events in play)\n"
        "  • 15:50 ET — close run (P&amp;L, score, lessons, tomorrow's watchlist)\n"
        "  • 20:00 ET — tomorrow preview (calendar + prior-adjusted watchlist)\n"
        "  • Sun 17:00 ET — week-ahead digest\n\n"
        "No more action needed — the bot runs itself."
    )


# ---------------------------------------------------------------------------
# XAUUSD swing-book messages (isolated formatters; equity Telegram unchanged)
# ---------------------------------------------------------------------------

def _gold_num(value, digits: int = 2, missing: str = "—") -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return missing


def _gold_hypothesis(trade: dict) -> dict:
    return trade.get("hypothesis") or {}


def format_swing_open(asof: str, opened: list, skipped: list | None = None,
                      *, fx_day: str | None = None, session: str | None = None,
                      atr: float | None = None) -> str:
    """Telegram payload for the XAUUSD swing entry and its initial risk."""
    lines = ["<b>🟢 GOLD — SWING OPEN</b>",
             f"<i>{_escape(asof)} · FX day {_escape(fx_day or '—')} · "
             f"{_escape(session or 'session unknown')}</i>", ""]
    if opened:
        for trade in opened:
            hyp = _gold_hypothesis(trade)
            side = str(trade.get("side") or "").upper()
            qty = _gold_num(trade.get("qty_oz") or trade.get("qty"), 3)
            entry = _gold_num(trade.get("entry_price"))
            stop = _gold_num(trade.get("stop_price"))
            risk = _gold_num(trade.get("risk_usd"))
            confidence = _as_float(hyp.get("confidence")) * 100
            lines.extend([
                f"<b>XAUUSD { _escape(side) }</b> · {qty} oz @ <code>${entry}</code>",
                f"Initial stop: <code>${stop}</code> · risk <b>${risk}</b> "
                f"({confidence:.0f}% thesis confidence)",
                f"Stop basis: {_escape(trade.get('stop_basis') or 'structure / ATR')} · "
                f"ATR(24h): ${_gold_num(atr or trade.get('atr_24h'))}",
                f"Thesis: {_escape(hyp.get('thesis') or '—')}",
            ])
            falsifiers = hyp.get("falsifiers") or []
            if isinstance(falsifiers, str):
                falsifiers = [falsifiers]
            lines.append(f"Falsifier: {_escape(falsifiers[0] if falsifiers else '—')}")
            target = trade.get("take_profit")
            lines.append(f"Target: ${_gold_num(target) if target else 'off'} · "
                         "no pyramiding · swing holds across FX days")
    else:
        lines.append("No entry — no XAUUSD hypothesis passed the configured gates.")
    if skipped:
        lines.append("")
        lines.append("<i>Skipped: " + _escape("; ".join(str(x) for x in skipped[:3])) + "</i>")
    lines.extend(["", "<i>Paper book only · no live broker · no daily flatten.</i>"])
    return "\n".join(lines)


def format_risk_check(asof: str, positions: list, updates: list,
                      events: list, *, fx_day: str | None = None,
                      block: str = "risk check", quote_available: bool = True,
                      stale_notes: list | None = None, paused: bool = False) -> str:
    """Two-per-FX-day status report: entry, stop/trail, mark P&L, events."""
    block_label = "LONDON" if block == "checkin_am" else "NY / PRE-BREAK"
    lines = ["<b>🟡 GOLD — RISK CHECK</b>",
             f"<i>{_escape(asof)} · FX day {_escape(fx_day or '—')} · {block_label}</i>", ""]
    if not quote_available:
        lines.extend(["⚠️ <b>Fresh quote unavailable.</b> Stop check ran, but no reliable "
                      "XAUUSD=X / GC=F price was available to compare.", ""])
    if positions:
        lines.append("<b>Open position(s):</b>")
        for row in positions:
            side = str(row.get("side") or "").upper()
            lines.append(f"• XAUUSD <b>{_escape(side)}</b> "
                         f"{_gold_num(row.get('qty_oz'), 3)} oz · uP&amp;L "
                         f"<b>{_as_float(row.get('upnl')):+,.2f}</b>")
            lines.append(f"  entry ${_gold_num(row.get('entry_price'))} · "
                         f"stop ${_gold_num(row.get('stop_price'))} · "
                         f"trail ${_gold_num(row.get('trail_price'))}")
            lines.append(f"  mark ${_gold_num(row.get('mark_price'))}")
    else:
        lines.append("<b>Open position(s):</b> none — book is flat")
    if updates:
        lines.extend(["", "<b>Trail adjustments:</b>"])
        for update in updates[:5]:
            old = _gold_num(update.get("old"))
            new = _gold_num(update.get("new"))
            reason = update.get("reason") or f"chandelier · ATR ${_gold_num(update.get('atr'))}"
            lines.append(f"• XAUUSD stop {old} → <b>${new}</b> ({_escape(reason)})")
    if events:
        lines.extend(["", "<b>Events in play:</b>"])
        for event in events[:5]:
            title = event.get("title") if isinstance(event, dict) else event
            lines.append("• " + _escape(title))
    else:
        lines.extend(["", "<b>Events in play:</b> none flagged"])
    if stale_notes:
        lines.extend(["", "<b>Thesis re-grade:</b>"])
        lines.extend("⚠️ " + _escape(note) for note in stale_notes[:3])
    if paused:
        lines.extend(["", "⏸ XAUUSD book is auto-paused; stop protection remains active."])
    lines.extend(["", "<i>Stops are checked every dispatch tick · no daily flatten.</i>"])
    return "\n".join(lines)


def format_exit(trade: dict) -> str:
    """Stop/target/thesis/weekend exit alert with realized grading."""
    reason = trade.get("exit_reason") or trade.get("status") or "closed"
    headline = {
        "stopped": "🛑 GOLD — STOPPED",
        "target": "🎯 GOLD — TARGET",
        "thesis_broken": "⚠️ GOLD — THESIS BROKEN",
        "weekend_flat": "🌙 GOLD — WEEKEND FLAT",
    }.get(reason, "⚪ GOLD — CLOSED")
    side = str(trade.get("side") or "").upper()
    qty = _gold_num(trade.get("qty_oz") or trade.get("qty"), 3)
    entry = _gold_num(trade.get("entry_price"))
    exit_price = _gold_num(trade.get("exit_price"))
    pnl = _as_float(trade.get("pnl"))
    pnl_pct = _as_float(trade.get("pnl_pct")) * 100
    grade = "✅ thesis held" if trade.get("hypothesis_correct") else "❌ thesis refuted"
    hyp = _gold_hypothesis(trade)
    lesson = trade.get("lesson") or "No additional lesson recorded."
    return "\n".join([
        f"<b>{headline}</b>",
        f"<i>{_escape(trade.get('closed_at') or '—')} · XAUUSD {side} · {qty} oz</i>",
        f"Entry <code>${entry}</code> → exit <code>${exit_price}</code>",
        f"Realized P&amp;L: <b>{pnl:+,.2f} ({pnl_pct:+.2f}%)</b>",
        f"Thesis grade: {_escape(grade)} · confidence {_as_float(hyp.get('confidence')) * 100:.0f}%",
        f"Lesson: {_escape(lesson)}",
        f"Exit reason: {_escape(reason)}",
    ])


def format_session_preview(asof: str, next_session: str, events: list,
                            hypotheses: list, positions: list, *,
                            paused: bool = False, price=None) -> str:
    """Next-session plan and levels, without modifying the swing book."""
    lines = ["<b>🌙 GOLD — SESSION PREVIEW</b>",
             f"<i>{_escape(asof)} · next session: {_escape(next_session)}</i>", ""]
    lines.append(f"Reference: ${_gold_num(price)}" if price else "Reference: quote unavailable")
    lines.extend(["", "<b>Open risk:</b>"])
    if positions:
        for row in positions[:2]:
            lines.append(f"• {_escape(str(row.get('side', '')).upper())} "
                         f"{_gold_num(row.get('qty_oz'), 3)} oz · stop "
                         f"${_gold_num(row.get('stop_price'))} · trail "
                         f"${_gold_num(row.get('trail_price'))}")
    else:
        lines.append("• Flat; no forced entry planned")
    lines.extend(["", "<b>Calendar:</b>"])
    lines.extend("• " + _escape(event) for event in (events or ["No high-impact event flagged."])[:8])
    lines.extend(["", "<b>Plan / levels:</b>"])
    tradeable = [h for h in hypotheses if h.get("tradeable")]
    if tradeable:
        for hyp in tradeable[:3]:
            lines.append(f"• {_escape(str(hyp.get('side', '')).upper())} · "
                         f"{_as_float(hyp.get('confidence')) * 100:.0f}% · "
                         f"{_escape(hyp.get('thesis') or '—')}")
    else:
        lines.append("• Wait for a qualifying thesis; initial structure/ATR stop required.")
    if paused:
        lines.extend(["", "⏸ Book paused; existing stops remain active."])
    lines.extend(["", "<i>Research changes do not auto-close positions; stale theses are re-graded.</i>"])
    return "\n".join(lines)


def format_fx_week_ahead(asof: str, week_days: list, events: list,
                          hypotheses: list, fred: dict | None = None, *,
                          paused: bool = False, open_positions: list | None = None) -> str:
    """Sunday gold macro digest with broad USD and real-yield context."""
    fred = fred or {}
    positions = open_positions or []
    if week_days:
        start, end = week_days[0].strftime("%b %d"), week_days[-1].strftime("%b %d")
    else:
        start = end = "—"
    lines = ["<b>📅 GOLD — WEEK AHEAD</b>", f"<i>{_escape(asof)}</i>", "",
             f"<b>FX week:</b> {_escape(start)} → {_escape(end)}", "",
             "<b>Macro backdrop:</b>"]
    usd = fred.get("DTWEXBGS") or {}
    real = fred.get("DFII10") or {}
    nominal = fred.get("DGS10") or {}
    if usd.get("value") is not None:
        lines.append(f"• Broad USD (DTWEXBGS): {_as_float(usd.get('value')):.2f}")
    else:
        lines.append("• Broad USD: FRED value unavailable")
    if real.get("value") is not None:
        lines.append(f"• 10Y real yield (DFII10): {_as_float(real.get('value')):.2f}%")
    else:
        lines.append("• 10Y real yield: FRED value unavailable")
    if nominal.get("value") is not None:
        lines.append(f"• 10Y nominal yield: {_as_float(nominal.get('value')):.2f}%")
    lines.extend(["", "<b>Central-bank / macro calendar:</b>"])
    lines.extend("• " + _escape(event) for event in (events or ["No major release flagged."])[:9])
    lines.extend(["", "<b>Gold plan:</b>"])
    tradeable = [h for h in hypotheses if h.get("tradeable")]
    if tradeable:
        for hyp in tradeable[:4]:
            lines.append(f"• {_escape(str(hyp.get('side', '')).upper())} · "
                         f"{_as_float(hyp.get('confidence')) * 100:.0f}% · "
                         f"{_escape(hyp.get('thesis') or '—')}")
            falsifiers = hyp.get("falsifiers") or []
            if falsifiers:
                lines.append("  Falsifier: " + _escape(falsifiers[0]))
    else:
        lines.append("• No tradeable XAUUSD thesis this pass.")
    lines.extend(["", "<b>Weekend policy:</b> flat before Friday 17:00 ET by default."])
    if positions:
        lines.append("⚠️ Open position remains; review the Friday risk-check policy.")
    if paused:
        lines.append("⏸ Book is currently auto-paused; this digest is informational.")
    lines.extend(["", "<i>Paper only · swap/carry ignored in phase 1.</i>"])
    return "\n".join(lines)


def format_fx_paused(reason: str, stats: str) -> str:
    return (f"<b>⏸ GOLD — AUTO-PAUSED</b>\n\n{_escape(reason)}\n\n"
            f"{_escape(stats)}\n\nExisting XAUUSD stops remain active. "
            "Review before resuming the paper book.")
