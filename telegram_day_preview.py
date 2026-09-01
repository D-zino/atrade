#!/usr/bin/env python3
"""Telegram day simulation — renders a 'typical trading day' exactly as the
agent's messages will appear in your Telegram chat, using the real message
formatters from atrade/telegram.py.

Output: atrade/telegram_day_simulation.html
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from atrade import telegram  # noqa: E402

# ---------------------------------------------------------------------------
# 1) Typical-day scenario data (Tuesday 2026-09-01 — realistic but fabricated)
# ---------------------------------------------------------------------------
OPEN_ASOF = "Tue Sep 1, 2026 · 09:25 ET (pre-market research)"
CLOSE_ASOF = "Tue Sep 1, 2026 · 15:50 ET (after close)"

opened = [
    {"symbol": "NVDA", "side": "long", "qty": 150, "entry_price": 1250.40,
     "hypothesis": {"confidence": 0.87}},
    {"symbol": "AMD", "side": "long", "qty": 120, "entry_price": 172.15,
     "hypothesis": {"confidence": 0.78}},
]
skipped = [
    "max positions (2) reached — TSLA short, MSFT long",
    "exposure cap — XLE long",
]

hyps = [
    {"symbol": "NVDA", "side": "long", "confidence": 0.87, "dominant_category": "earnings",
     "falsifiers": ["NVDA gaps/breaks against thesis (more than ~0.8% adverse move)",
                    "a stronger opposing catalyst (Fed/macro/earnings) prints during the day"]},
    {"symbol": "AMD", "side": "long", "confidence": 0.78, "dominant_category": "earnings",
     "falsifiers": ["AMD breaks day range against thesis after any downgrade headline"]},
    {"symbol": "TSLA", "side": "short", "confidence": 0.72, "dominant_category": "sentiment",
     "falsifiers": ["retail sentiment flips; delivery beat or China approval news"]},
    {"symbol": "MSFT", "side": "long", "confidence": 0.68, "dominant_category": "tech",
     "falsifiers": ["capex-cut headline from any hyperscaler"]},
    {"symbol": "XLE", "side": "long", "confidence": 0.63, "dominant_category": "commodities",
     "falsifiers": ["OPEC+ surprise output increase; WTI breaks below $70"]},
    {"symbol": "SPY", "side": "long", "confidence": 0.61, "dominant_category": "macro",
     "falsifiers": ["10Y yield jumps >4.7% on hawkish Fed speaker"]},
    {"symbol": "QQQ", "side": "long", "confidence": 0.60, "dominant_category": "macro",
     "falsifiers": ["semis roll over; Nasdaq breadth weakens"]},
    {"symbol": "GLD", "side": "long", "confidence": 0.58, "dominant_category": "commodities",
     "falsifiers": ["gold breaks below $2,480 support"]},
]

closed = [
    {"symbol": "NVDA", "side": "long", "pnl": 1284.00, "pnl_pct": 0.0068, "hypothesis_correct": 1},
    {"symbol": "AMD", "side": "long", "pnl": -412.30, "pnl_pct": -0.0195, "hypothesis_correct": 0},
]
lessons = [
    "Winning trades clustered around 'earnings' evidence (1 of 1 winners) — this signal category is earning its prior.",
    "Losers were dominated by 'analyst' (1 of 1) — reducing prior weight for that category until it demonstrates edge.",
    "NVDA: long move of +0.68% (vindicated) a 87%-confidence thesis.",
    "AMD: long move of -1.95% (against) a 78%-confidence thesis. Falsifier used: AMD broke day range after downgrade headline.",
]
tracker = {
    "earnings": {"n": 8, "win_rate": 0.75},
    "macro": {"n": 5, "win_rate": 0.60},
    "commodities": {"n": 2, "win_rate": 0.50},
    "technical": {"n": 10, "win_rate": 0.40},
    "analyst": {"n": 3, "win_rate": 0.33},
}

# ---------------------------------------------------------------------------
# 2) Build the actual messages with the real formatters
# ---------------------------------------------------------------------------
msg_open = telegram.format_open(OPEN_ASOF, "alpaca_paper", 30, opened, skipped, hyps)
msg_close = telegram.format_close(CLOSE_ASOF, "alpaca_paper", closed, 0.6120, True,
                                  871.70, lessons, tracker, hyps)
msg_pause = telegram.format_paused(
    "no_improve_streak=6 | failed_measure=0",
    "score 0.5230 | best 0.6120")

# All five messages are now wired to the scheduler — none are optional.
msg_checkin = (
    "<b>🟡 A-TRADE — MID-SESSION CHECK-IN</b>\n"
    "<i>Tue Sep 1 · 10:30 ET</i>\n\n"
    "<b>Open positions:</b>\n"
    "  • NVDA <b>LONG</b> 150 sh — +1.2% (+$2,253) 🟢\n"
    "  • AMD <b>LONG</b> 120 sh — -0.6% (-$124) 🔴\n\n"
    "<b>Events in play:</b>\n"
    "  • Fed's Williams speaking 10:00 ET — hawkish tone would pressure tech\n"
    "  • 30-yr Treasury auction 13:00 ET\n"
    "  • OPEC+ monthly report due\n\n"
    "<i>Day-trade policy: positions are held to the close run. No action needed now.</i>"
)
msg_preview = (
    "<b>🌙 A-TRADE — TOMORROW PREVIEW</b>\n"
    "<i>Tue Sep 1 · 20:00 ET</i>\n\n"
    "<b>Tomorrow (Wed Sep 2) on the calendar:</b>\n"
    "  • 08:30 ET — Initial Jobless Claims\n"
    "  • 10:00 ET — Fed's Waller speech\n"
    "  • 13:00 ET — 10-yr note auction\n\n"
    "<b>Watchlist with confidence (already prior-adjusted from today's grading):</b>\n"
    "  • NVDA <b>LONG</b> — 81% (earnings drift still paying)\n"
    "  • TSLA <b>SHORT</b> — 68% (delivery fears; sentiment bearish)\n"
    "  • MSFT <b>LONG</b> — 65% (Azure momentum)\n"
    "  • XLE <b>LONG</b> — 63% (oil must hold $70)\n"
    "  • AMD <b>LONG</b> — 62% (downgrade pressure; needs reversal)\n\n"
    "<b>Theses would change if:</b>\n"
    "  • NVDA: any AI-capex-cut headline from hyperscalers\n"
    "  • TSLA: delivery beat or China approval news\n"
    "  • XLE: OPEC+ surprise output increase\n"
    "  • AMD: upgrade/PT raise or strong short-covering tape\n\n"
    "<i>These are experiments — every thesis lists its falsifiers in the full reports.</i>"
)

# Sunday week-ahead digest (sample week: Mon Sep 7 - Fri Sep 11, 2026)
msg_week = telegram.format_week_ahead(
    "Sun Sep 6, 2026 · 17:00 ET",
    [__import__("datetime").date(2026, 9, 7) + __import__("datetime").timedelta(days=i) for i in range(5)],
    ["Thu Sep 10 — 08:30 ET Jobless Claims",
     "Fri Sep 11 — no major releases; watch Fed speakers",
     "CPI likely this week (BLS mid-month window, 08:30 ET)"],
    hyps,
    {"DGS10": {"value": 4.52}, "DGS2": {"value": 3.98}, "FEDFUNDS": {"value": 3.75}},
    None,
)

# ---------------------------------------------------------------------------
# 3) Render a Telegram-style chat thread
# ---------------------------------------------------------------------------
CSS = """
* { box-sizing: border-box; }
body { margin:0; background:#0e1621; color:#fff; font-family:-apple-system,'Segoe UI',
       Roboto,Helvetica,Arial,sans-serif; }
.pagehead { max-width:760px; margin:0 auto; padding:26px 18px 6px; }
.pagehead h1 { font-size:20px; margin:0 0 6px; }
.pagehead p { color:#8ea1b3; font-size:13px; margin:4px 0; line-height:1.5; }
.legend { display:flex; gap:8px; margin:10px 0 4px; flex-wrap:wrap; }
.pill { font-size:11px; padding:3px 10px; border-radius:20px; }
.pill.now { background:#16281c; color:#64d98b; border:1px solid #2d5a3f; }
.pill.opt { background:#2d2305; color:#e8b53a; border:1px solid #6b5517; }
.chat { max-width:760px; margin:0 auto; padding:14px 18px 40px; }
.daydiv { text-align:center; margin:18px 0; }
.daydiv span { background:#182533; color:#8ea1b3; font-size:11px; letter-spacing:1px;
       padding:4px 12px; border-radius:12px; text-transform:uppercase; }
.msg { display:flex; gap:10px; margin:14px 0; }
.avatar { width:40px; height:40px; border-radius:50%; background:linear-gradient(135deg,#2b5278,#1f3a55);
       display:flex; align-items:center; justify-content:center; font-size:20px; flex-shrink:0; }
.bubble { background:#182533; border-radius:14px; padding:8px 12px 6px; max-width:85%;
       font-size:13.5px; line-height:1.55; white-space:pre-wrap; overflow-wrap:anywhere; }
.sender { color:#53b4f4; font-size:12.5px; font-weight:600; margin-bottom:2px; }
.bubble b { font-weight:700; }
.time { color:#6f8294; font-size:11px; text-align:right; margin-top:4px; }
.tag { float:right; margin:6px 0 0 8px; font-size:9.5px; padding:2px 7px; border-radius:10px; }
.tag.now { background:#16381f; color:#64d98b; }
.tag.opt { background:#3a2c05; color:#e8b53a; }
.note { max-width:760px; margin:0 auto; padding:0 18px 30px; color:#8ea1b3; font-size:12.5px; line-height:1.6; }
.note code { background:#182533; padding:1px 6px; border-radius:6px; color:#e6edf3; }
"""


def bubble(title, text, tag_cls, tag_label, time, avatar="🤖", sender="A-Trade Bot"):
    tag = f'<span class="tag {tag_cls}">{tag_label}</span>'
    return f"""<div class="msg">
  <div class="avatar">{avatar}</div>
  <div style="flex:1">
    <div class="bubble">{tag}<div class="sender">{sender}</div>{text}
      <div class="time">{time}</div></div>
  </div>
</div>"""


def escape_html(text: str) -> str:
    """The formatter output is HTML — keep <b>/<i>, escape nothing else needed."""
    return text


body = f"""
<div class="pagehead">
  <h1>📱 A-Trade Telegram — a typical trading day, simulated</h1>
  <p>This is exactly what the bot sends — generated with the <b>real message
     formatters</b> from the code, using realistic sample data for a Tuesday
     (2026-09-01). <b>All six messages ship automatically</b> once you add your
     Telegram keys: four daily (09:25, 10:30, 15:50, 20:00 ET), the Sunday
     17:00 ET week-ahead digest, plus the pause alert if the agent stalls.</p>
</div>
<div class="chat">

  <div class="daydiv"><span>Tue · Sep 1 · Morning</span></div>
  {bubble("open", escape_html(msg_open), "now", "SHIPS NOW", "09:25")}

  {bubble("checkin", escape_html(msg_checkin), "now", "SHIPS NOW", "10:30")}

  <div class="daydiv"><span>Tue · Sep 1 · Evening</span></div>
  {bubble("close", escape_html(msg_close), "now", "SHIPS NOW", "15:50")}

  {bubble("preview", escape_html(msg_preview), "now", "SHIPS NOW", "20:00")}

  <div class="daydiv"><span>Sun · Sep 6 · Weekly</span></div>
  {bubble("week", escape_html(msg_week), "now", "SHIPS NOW", "17:00")}

  <div class="daydiv"><span>Only if the agent auto-pauses</span></div>
  {bubble("pause", escape_html(msg_pause), "now", "SHIPS NOW", "16:05")}

</div>
<div class="note">
  <b>How to turn this into reality:</b> put <code>TELEGRAM_BOT_TOKEN</code> and
  <code>TELEGRAM_CHAT_ID</code> in <code>atrade/.env</code> (or repo secrets on
  GitHub Actions), then <code>python3 -m atrade.cli notify</code> to test.
  On GitHub Actions the workflow runs all four sessions automatically; you can
  also trigger any of them instantly from the <b>Run workflow</b> menu.
</div>
"""

html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A-Trade — Telegram day simulation</title><style>{CSS}</style></head>
<body>{body}</body></html>"""

out = ROOT / "telegram_day_simulation.html"
out.write_text(html)
print(f"wrote {out}")

# also dump the raw messages for easy copy-paste
print("\n" + "=" * 72)
print("RAW MESSAGE TEXTS")
print("=" * 72)
for label, m in [("OPEN (09:25 ET)", msg_open), ("CLOSE (15:50 ET)", msg_close),
                 ("PAUSE", msg_pause)]:
    print(f"\n########## {label} ##########\n{m}")
