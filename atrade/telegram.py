"""Optional Telegram notifications via the Bot API.

To enable, add to atrade/.env:
    TELEGRAM_BOT_TOKEN=123456:ABC...     (from @BotFather)
    TELEGRAM_CHAT_ID=123456789           (your chat/user id; see README)

Without credentials every call degrades gracefully to a no-op — the agent
never fails because Telegram is unreachable.
"""
from __future__ import annotations

import urllib.parse
import urllib.request

from . import config as config_mod, market, util


def _keys() -> dict:
    return config_mod.load_env_keys()


def configured() -> bool:
    k = _keys()
    return bool(k.get("TELEGRAM_BOT_TOKEN") and k.get("TELEGRAM_CHAT_ID"))


def send(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message; returns True if delivered. No-op when not configured."""
    k = _keys()
    token, chat = k.get("TELEGRAM_BOT_TOKEN"), k.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        util.log("Telegram not configured — skipping notification", "DEBUG")
        return False
    ok = True
    for chunk in (text[i:i + 3950] for i in range(0, len(text), 3950)):  # API limit 4096
        data = urllib.parse.urlencode({
            "chat_id": chat, "text": chunk, "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
        }).encode()
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except Exception as e:
            util.log(f"telegram send failed: {e}", "WARN")
            ok = False
    return ok


# ---------------------------------------------------------------------------
# Message builders
# ---------------------------------------------------------------------------
def _falsifier(h: dict) -> str:
    fals = h.get("falsifiers") or []
    return fals[0] if fals else "—"


def format_open(asof: str, mode: str, universe_size: int, opened: list,
                skipped: list, hyps: list) -> str:
    lines = [
        "<b>🟢 A-TRADE — OPEN RUN</b>",
        f"<i>{asof} · {mode} mode</i>",
        "",
    ]
    if opened:
        lines.append("<b>Opened today:</b>")
        for t in opened:
            h = t.get("hypothesis") or {}
            lines.append(f"  • {t['symbol']} <b>{t['side'].upper()}</b> {t['qty']} sh "
                         f"@ ${t.get('entry_price', 0):,.2f} (conf {h.get('confidence', 0) * 100:.0f}%)")
    else:
        lines.append("<b>Opened today:</b> none — no hypothesis ≥ 60% confidence")
    if skipped:
        lines.append(f"<i>   skipped: {'; '.join(str(s)[:70] for s in skipped[:3])}</i>")
    lines.append("")
    lines.append(f"<b>Scanning {universe_size} symbols</b> + FRED macro (yields/CPI/PPI/jobs) "
                 "+ SEC filings (insider Form 4s) + news + FX")
    lines.append("")
    lines.append("<b>Active hypotheses / watchlist (ranked by confidence):</b>")
    for h in hyps[:8]:
        lines.append(f"  • {h['symbol']} <b>{h['side'].upper()}</b> — conf "
                     f"{h['confidence'] * 100:.0f}% · {h.get('dominant_category', '?')}")
    lines.append("")
    lines.append("<i>Falsifies if: " + _falsifier((hyps[0] if hyps else {})) + "</i>" if hyps else "")
    return "\n".join(lines)


def format_close(asof: str, mode: str, closed: list, score, improved: bool,
                 net_pnl: float, lessons: list, tracker: dict, hyps: list) -> str:
    lines = [
        "<b>🔴 A-TRADE — CLOSE RUN</b>",
        f"<i>{asof} · {mode} mode</i>",
        "",
    ]
    if closed:
        lines.append("<b>Day trades closed:</b>")
        for t in closed:
            mark = "✅" if t.get("hypothesis_correct") else "❌"
            lines.append(f"  • {t['symbol']} <b>{t['side'].upper()}</b> "
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
            lines.append(f"  • {str(l)[:150]}")
    cats = sorted([c for c in tracker if not c.startswith("_")],
                  key=lambda c: -(tracker[c].get("win_rate") or 0))[:5]
    if cats:
        lines.append("")
        lines.append("<b>Signal tracker (learned edge):</b>")
        for c in cats:
            st = tracker[c]
            wr = f"{st['win_rate'] * 100:.0f}%" if st.get("win_rate") is not None else "n/a"
            lines.append(f"  • {c}: n={st.get('n', 0)} win={wr}")
    lines.append("")
    lines.append("<b>Watchlist for tomorrow / next session:</b>")
    for h in hyps[:6]:
        lines.append(f"  • {h['symbol']} <b>{h['side'].upper()}</b> — conf "
                     f"{h['confidence'] * 100:.0f}% · {h.get('dominant_category', '?')}")
    lines.append("")
    lines.append("<i>See PLAYBOOK.md for the full updated playbook.</i>")
    return "\n".join(lines)


def format_checkin(asof: str, mode: str, positions: list, event_notes: list) -> str:
    """Mid-session check-in: open positions with unrealized P&L + events in play."""
    lines = [
        "<b>🟡 A-TRADE — MID-SESSION CHECK-IN</b>",
        f"<i>{asof} · {mode} mode</i>",
        "",
    ]
    if positions:
        lines.append("<b>Open positions:</b>")
        for p in positions:
            sym = p.get("symbol")
            qty = int(p.get("qty") or 0)
            upnl = float(p.get("unrealized_pl") or 0)
            side = "LONG" if qty > 0 else "SHORT"
            arrow = "🟢" if upnl >= 0 else "🔴"
            lines.append(f"  • {sym} <b>{side}</b> {abs(qty)} sh — {upnl:+,.2f} {arrow}")
    else:
        lines.append("<b>Open positions:</b> none (all flat)")
    lines.append("")
    if event_notes:
        lines.append("<b>Events in play:</b>")
        for n in event_notes[:4]:
            lines.append(f"  • {n.get('title')}")
    else:
        lines.append("<b>Events in play:</b> none flagged")
    lines.append("")
    lines.append("<i>Day-trade policy: positions are held to the close run. No action needed now.</i>")
    return "\n".join(lines)


def format_preview(asof: str, next_day: str, events: list, hyps: list) -> str:
    """Tomorrow preview: next trading day, calendar, prior-adjusted watchlist."""
    lines = [
        "<b>🌙 A-TRADE — TOMORROW PREVIEW</b>",
        f"<i>{asof}</i>",
        "",
        f"<b>Next trading day:</b> {next_day}",
        "",
        "<b>On the calendar:</b>",
    ]
    for e in events:
        lines.append(f"  • {e}")
    lines.append("")
    lines.append("<b>Watchlist with confidence (prior-adjusted from today's grading):</b>")
    if hyps:
        for h in hyps[:8]:
            lines.append(f"  • {h['symbol']} <b>{h['side'].upper()}</b> — "
                         f"{h['confidence'] * 100:.0f}% ({h.get('dominant_category', '?')})")
    else:
        lines.append("  • none — no signal-rich setups tonight")
    lines.append("")
    lines.append("<b>Theses would change if:</b>")
    for h in hyps[:4]:
        fals = (h.get("falsifiers") or ["new contradictory information"])
        lines.append(f"  • {h['symbol']}: {fals[0]}")
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
        f"<i>{asof}</i>",
        "",
        f"<b>Next trading week:</b> {market.date_str(monday)} → {market.date_str(friday)}",
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
        lines.append(f"  • {e}")
    lines.append("")
    lines.append("<b>Week watchlist (conf = prior-adjusted):</b>")
    for h in hyps[:8]:
        lines.append(f"  • {h['symbol']} <b>{h['side'].upper()}</b> — "
                     f"{h['confidence'] * 100:.0f}% ({h.get('dominant_category', '?')})")
    if not hyps:
        lines.append("  • none flagged this week")
    lines.append("")
    lines.append("<b>Biggest falsifier risks this week:</b>")
    for h in hyps[:3]:
        fals = (h.get("falsifiers") or ["new contradictory information"])[0]
        lines.append(f"  • {h['symbol']}: {fals}")
    lines.append("")
    if paused_note:
        lines.append(paused_note)
        lines.append("")
    lines.append("<i>Weekly setup; the daily 09:25 / 10:30 / 15:50 / 20:00 ET "
                 "messages refine it day by day.</i>")
    return "\n".join(lines)


def format_paused(reason: str, stats: str) -> str:
    return (f"<b>⏸ A-TRADE — AUTO-PAUSED</b>\n\n{reason}\n\n{stats}\n\n"
            f"Trading is halted. Review, then resume with: python3 -m atrade.cli resume")


def format_error(run_type: str, err: str) -> str:
    return f"<b>⚠️ A-TRADE — {run_type.upper()} RUN ERROR</b>\n\n<code>{str(err)[:1000]}</code>"
