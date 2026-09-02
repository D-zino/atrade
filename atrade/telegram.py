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
