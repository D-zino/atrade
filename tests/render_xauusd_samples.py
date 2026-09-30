"""Render Telegram-compatible XAUUSD payloads as a standalone HTML preview."""
from __future__ import annotations

import html
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from atrade import telegram

OUTPUT = ROOT / "docs" / "XAUUSD_TELEGRAM_SAMPLES.html"


def render() -> str:
    opened = {
        "trade_id": "XAUUSD-2026-09-28-083000", "symbol": "XAUUSD", "side": "long",
        "qty": 4.685, "qty_oz": 4.685, "entry_price": 4200.15,
        "stop_price": 3986.72, "trail_price": None, "take_profit": None,
        "risk_usd": 999.92, "stop_basis": "3x_atr_tighter_than_pivot",
        "atr_24h": 71.14, "status": "open", "fx_day": "2026-09-28",
        "opened_at": "2026-09-28T08:30:00-04:00",
        "hypothesis": {
            "thesis": "Go long XAUUSD: safe-haven demand and softer real yields support gold.",
            "falsifiers": ["underlying commodity reverses its move by more than 1%"],
            "confidence": 0.742, "dominant_category": "commodities",
        },
    }
    risk_rows = [{
        "symbol": "XAUUSD", "side": "long", "qty_oz": 4.685,
        "entry_price": 4200.15, "stop_price": 3986.72, "trail_price": 3997.44,
        "upnl": 45.64, "mark_price": 4210.00,
    }]
    exit_trade = {
        **opened, "status": "stopped", "exit_reason": "stopped",
        "exit_price": 3984.77, "closed_at": "2026-09-29T02:15:00-04:00",
        "pnl": -1008.15, "pnl_pct": -0.0513, "hypothesis_correct": 0,
        "lesson": "Gold stopped below the confirmed swing/ATR level; initial risk remained near 1% before gap/slippage.",
    }
    hyp = {
        "symbol": "XAUUSD", "side": "long", "confidence": 0.742,
        "tradeable": True,
        "thesis": "Gold demand firms while real yields ease.",
        "falsifiers": ["real yields reverse and gold closes below the swing low"],
    }
    samples = [
        ("Swing open", telegram.format_swing_open(
            "2026-09-28T08:30:00-04:00", [opened], [], fx_day="2026-09-28",
            session="overlap", atr=71.14)),
        ("Risk check", telegram.format_risk_check(
            "2026-09-28T14:15:00-04:00", risk_rows,
            [{"old": None, "new": 3997.44, "atr": 71.14,
              "reason": "chandelier · ATR $71.14"}],
            ["Fri Oct 02 — 08:30 ET Nonfarm Payrolls (NFP) [upcoming]"], fx_day="2026-09-28",
            block="checkin_pm", quote_available=True)),
        ("Stop exit", telegram.format_exit(exit_trade)),
        ("Session preview", telegram.format_session_preview(
            "2026-09-28T18:05:00-04:00", "asia",
            ["Tue Sep 29 — No major scheduled release flagged"], [hyp], risk_rows,
            paused=False, price=4215.00)),
        ("Week ahead", telegram.format_fx_week_ahead(
            "2026-09-27T18:05:00-04:00", [date(2026, 9, 28) + timedelta(days=i)
                                           for i in range(5)],
            ["Fri Oct 02 — 08:30 ET Nonfarm Payrolls (NFP)",
             "No additional red releases flagged in this deterministic fixture."], [hyp],
            {"DTWEXBGS": {"value": 120.2}, "DFII10": {"value": 1.72},
             "DGS10": {"value": 4.03}}, paused=False, open_positions=[])),
        ("Auto-pause", telegram.format_fx_paused(
            "XAUUSD max drawdown exceeded: 14.2% below peak (limit 14.0%).",
            "XAUUSD paper equity 85,800.00")),
    ]
    cards = []
    for title, message in samples:
        # Telegram payload is already safely escaped by its formatter; the
        # supported tags (<b>, <i>, <code>) are intentionally rendered here.
        cards.append(f"""<section class="sample">
  <div class="label">{html.escape(title)}</div>
  <div class="bubble">{message.replace(chr(10), '<br>')}</div>
</section>""")
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>XAUUSD Telegram message samples</title>
<style>
:root{color-scheme:dark;font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;background:#0e1721;color:#e7edf4}
*{box-sizing:border-box}body{margin:0;padding:32px 18px 52px;background:radial-gradient(ellipse at top,#172b3b,#0e1721 58%)}
main{max-width:880px;margin:0 auto}h1{font-size:clamp(1.5rem,4vw,2.4rem);margin:0 0 8px;letter-spacing:-.035em}
.sub{color:#9eb0c1;margin:0 0 28px;line-height:1.6}.sample{margin:20px 0 26px}.label{font-size:.78rem;text-transform:uppercase;letter-spacing:.13em;color:#89a6bd;margin:0 0 8px 8px}
.bubble{max-width:680px;background:#182633;border:1px solid #2b4152;border-radius:18px 18px 18px 5px;padding:18px 20px;line-height:1.55;box-shadow:0 12px 34px #0004;overflow-wrap:anywhere}
.bubble b{color:#fff}.bubble i{color:#aab8c4}.bubble code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#c6e9ff;background:#10202c;padding:1px 4px;border-radius:4px}
.note{margin-top:30px;padding:14px 16px;border-left:3px solid #5cb7a8;color:#afc1ce;background:#13212c;border-radius:5px}
</style>
</head>
<body><main>
<h1>Gold book · Telegram previews</h1>
<p class="sub">Rendered from the production <code>atrade.telegram</code> formatters. Paper-only XAUUSD swing book; escaped HTML and Telegram-supported formatting are shown as delivered.</p>
""" + "\n".join(cards) + """
<div class="note">All prices, positions, P&amp;L, and macro entries on this page are deterministic preview fixtures—not live recommendations or real trades.</div>
</main></body></html>
"""


if __name__ == "__main__":
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(render())
    print(OUTPUT)
