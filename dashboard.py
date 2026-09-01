"""Generate a self-contained dashboard.html from live state files.

Reads: state/state.json, PLAYBOOK.md, state/reports/*.md, trades_ledger.json.
Output: atrade/dashboard.html — zero external dependencies (inline CSS).
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"


def _load(name, default=None):
    p = STATE / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _md_to_html(md: str) -> str:
    """Ultra-light markdown → HTML (headings, tables, lists, bold)."""
    lines = md.splitlines()
    html, in_table, in_list = [], False, False
    for ln in lines:
        s = ln.strip()
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        if s.startswith("# "):
            html.append(f"<h1>{s[2:]}</h1>")
        elif s.startswith("## "):
            html.append(f"<h2>{s[3:]}</h2>")
        elif s.startswith("|") and s.endswith("|"):
            cells = [c.strip() for c in s.strip("|").split("|")]
            if not in_table:
                html.append("<table><tr>" + "".join(f"<th>{c}</th>" for c in cells) + "</tr>")
                in_table = True
            elif all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            else:
                html.append("<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>")
        else:
            if in_table:
                html.append("</table>")
                in_table = False
            if s.startswith("- "):
                if not in_list:
                    html.append("<ul>")
                    in_list = True
                html.append(f"<li>{s[2:]}</li>")
            elif s:
                if in_list:
                    html.append("</ul>")
                    in_list = False
                html.append(f"<p>{s}</p>")
    if in_table:
        html.append("</table>")
    if in_list:
        html.append("</ul>")
    return "\n".join(html)


def build() -> str:
    import atrade.config as config_mod
    cfg = config_mod.load_config()
    state = _load("state.json") or {}
    ledger = state.get("ledger", [])
    baseline = state.get("baseline") or {}
    metrics = state.get("last_metrics") or {}
    comps = state.get("last_components") or {}
    score = state.get("last_score")
    history = state.get("score_history", [])
    tracker = state.get("signal_tracker", {})
    streaks = state.get("streaks", {})
    thinking = state.get("thinking_history", [])
    resume = state.get("resume", {})

    bench = (baseline.get("benchmarks") or {})
    spy0 = (bench.get("SPY") or {}).get("price")
    qqq0 = (bench.get("QQQ") or {}).get("price")
    reports = sorted((STATE / "reports").glob("*.md")) if (STATE / "reports").exists() else []

    # --- positions from broker snapshot (mock) or Alpaca
    mock_acct = _load("mock_account.json") or {}
    positions = mock_acct.get("positions", {}) or {}
    pos_rows = ""
    if positions:
        for sym, p in positions.items():
            pos_rows += f"<tr><td>{sym}</td><td>{p['qty']}</td></tr>"
    else:
        pos_rows = "<tr><td colspan='2' class='muted'>none (flat)</td></tr>"

    # --- ledger table
    led_rows = ""
    for t in ledger:
        hyp = t.get("hypothesis") or {}
        conf = hyp.get("confidence")
        correct = "✅" if t.get("hypothesis_correct") else "❌"
        led_rows += (
            f"<tr><td>{t.get('symbol')}</td><td>{t.get('side')}</td><td>{t.get('qty')}</td>"
            f"<td>{t.get('entry_price') if t.get('entry_price') is not None else '-'}</td>"
            f"<td>{t.get('exit_price') if t.get('exit_price') is not None else '-'}</td>"
            f"<td class=\"{'pos' if (t.get('pnl') or 0) >= 0 else 'neg'}\">{t.get('pnl'):+,.2f}</td>"
            f"<td>{f'{conf*100:.0f}%' if conf is not None else '-'}</td>"
            f"<td>{correct}</td><td>{(hyp.get('dominant_category') or '-')}</td></tr>"
        )
    if not led_rows:
        led_rows = "<tr><td colspan='9' class='muted'>no trades yet</td></tr>"

    # --- tracker table
    tr_rows = ""
    for cat in sorted([c for c in tracker if not c.startswith("_")],
                      key=lambda c: -(tracker[c].get("win_rate") or 0)):
        st = tracker[cat]
        wr = f"{st['win_rate']*100:.0f}%" if st.get("win_rate") is not None else "n/a"
        edge = f"${st['edge']:+,.0f}" if st.get("edge") is not None else "n/a"
        tr_rows += f"<tr><td>{cat}</td><td>{st.get('n', 0)}</td><td>{wr}</td><td>{edge}</td></tr>"
    if not tr_rows:
        tr_rows = "<tr><td colspan='4' class='muted'>empty — first graded trades will populate this</td></tr>"

    # --- thinking history
    think_html = "".join(f"<li><b>{x.get('date')}</b>: {x.get('change')}</li>" for x in thinking[-5:])
    if not think_html:
        think_html = "<li class='muted'>no thinking history yet</li>"

    # --- score sparkline
    spark = ""
    if history:
        vals = [h.get("score") or 0 for h in history]
        w, hpx = max(40, len(vals) * 40), 60
        maxv = max(vals) if vals else 1
        pts = []
        for i, v in enumerate(vals):
            x = i * (w / max(1, len(vals) - 1)) + 10
            y = hpx - 8 - (v / max(maxv, 0.001)) * (hpx - 20)
            pts.append(f"{x:.0f},{y:.0f}")
        poly = " ".join(pts)
        spark = (f"<svg width='{w+20}' height='{hpx}' viewBox='0 0 {w+20} {hpx}' "
                 f"style='background:#0d1117;border-radius:8px'>"
                 f"<polyline points='{poly}' fill='none' stroke='#58a6ff' stroke-width='2'/></svg>")

    # --- report cards
    rep_cards = ""
    for r in reversed(reports[-6:]):
        kind = "OPEN" if "open_" in r.name else "CLOSE"
        color = "#238636" if kind == "OPEN" else "#1f6feb"
        rep_cards += (f"<a class='card' style='border-top:3px solid {color}' "
                      f"href='state/reports/{r.name}'>"
                      f"<div class='card-k'>{kind}</div><div class='card-t'>{r.stem}</div></a>")
    if not rep_cards:
        rep_cards = "<p class='muted'>no reports yet</p>"

    playbook_html = ""
    if (ROOT / "PLAYBOOK.md").exists():
        playbook_html = _md_to_html((ROOT / "PLAYBOOK.md").read_text())

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>A-Trade — autonomous paper-trading agent</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
         background:#010409; color:#e6edf3; line-height:1.5; }}
  header {{ padding:22px 26px; background:linear-gradient(135deg,#0d1117,#161b22);
           border-bottom:1px solid #21262d; }}
  header h1 {{ margin:0; font-size:22px; }}
  header .sub {{ color:#8b949e; font-size:13px; margin-top:4px; }}
  .badges {{ margin-top:10px; }}
  .badge {{ display:inline-block; padding:3px 10px; border-radius:20px; font-size:12px;
           margin-right:8px; border:1px solid #30363d; }}
  .badge.green {{ background:#12261a; color:#3fb950; border-color:#238636; }}
  .badge.amber {{ background:#2d1b05; color:#d29922; border-color:#9e6a03; }}
  .badge.red {{ background:#3d0c0c; color:#f85149; border-color:#da3633; }}
  .badge.blue {{ background:#0d1d33; color:#58a6ff; border-color:#1f6feb; }}
  main {{ max-width:1100px; margin:0 auto; padding:24px 26px; }}
  h2 {{ font-size:16px; border-bottom:1px solid #21262d; padding-bottom:8px; margin-top:34px; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:12px; }}
  .card {{ background:#0d1117; border:1px solid #21262d; border-radius:10px; padding:14px; }}
  a.card {{ color:inherit; text-decoration:none; display:block; }}
  .kpi .v {{ font-size:24px; font-weight:700; }}
  .kpi .l {{ font-size:12px; color:#8b949e; margin-top:2px; }}
  table {{ width:100%; border-collapse:collapse; background:#0d1117; border-radius:10px;
          overflow:hidden; font-size:13px; }}
  th, td {{ padding:8px 10px; text-align:left; border-bottom:1px solid #21262d; }}
  th {{ background:#161b22; color:#8b949e; font-weight:600; font-size:12px; text-transform:uppercase; }}
  .pos {{ color:#3fb950; }} .neg {{ color:#f85149; }} .muted {{ color:#8b949e; }}
  section {{ margin-top:26px; }}
  .playbook {{ background:#0d1117; border:1px solid #21262d; border-radius:10px; padding:18px 22px; font-size:14px; }}
  .playbook h1 {{ font-size:18px; }} .playbook h2 {{ font-size:15px; border:none; margin-top:18px; }}
  .playbook table {{ margin:10px 0; }}
  ul {{ padding-left:20px; }}
  footer {{ color:#484f58; font-size:12px; text-align:center; padding:30px; }}
</style></head><body>
<header>
  <h1>🤖 A-Trade — Self-Improving Autonomous Paper-Trading Agent</h1>
  <div class="sub">Research → hypotheses → simulated day trades → graded experiments → living playbook</div>
  <div class="badges">
    <span class="badge {'green' if not resume.get('paused') else 'red'}">{'RUNNING' if not resume.get('paused') else 'PAUSED'}</span>
    <span class="badge blue">mode: {cfg.get('broker', 'alpaca')} (paper)</span>
    <span class="badge">last score: <b>{score if score is not None else 'n/a'}</b></span>
    <span class="badge">best: <b>{max((h.get('score') or 0) for h in history) if history else 'n/a'}</b></span>
    <span class="badge amber">no-improve streak: {streaks.get('no_improve', 0)}/6</span>
  </div>
</header>
<main>
  <h2>Portfolio & performance (vs frozen SPY/QQQ baseline)</h2>
  <div class="grid">
    <div class="card kpi"><div class="v {'pos' if (metrics.get('net_pnl') or 0) >= 0 else 'neg'}">{metrics.get('net_pnl', 0):+,.2f}</div><div class="l">Realized P&L</div></div>
    <div class="card kpi"><div class="v">{f'{metrics.get("portfolio_return", 0)*100:+.2f}%'}</div><div class="l">Portfolio return</div></div>
    <div class="card kpi"><div class="v">{f'{metrics.get("alpha", 0)*100:+.2f}%'}</div><div class="l">Alpha vs SPY (baseline {spy0 if spy0 else 'n/a'})</div></div>
    <div class="card kpi"><div class="v">{metrics.get('win_rate', 0)*100:.0f}%</div><div class="l">Win rate ({metrics.get('n_trades', 0)} closed)</div></div>
    <div class="card kpi"><div class="v">{metrics.get('sharpe', 'n/a') if metrics.get('sharpe') is not None else 'n/a'}</div><div class="l">Sharpe (annualized)</div></div>
    <div class="card kpi"><div class="v">{score if score is not None else 'n/a'}</div><div class="l">Composite (ret 35% / alpha 30% / sharpe 20% / win 15%)</div></div>
  </div>
  {f'<div style="margin-top:14px">{spark}<div class="muted" style="font-size:12px">composite score over close runs</div></div>' if spark else ''}

  <h2>Open positions</h2>
  <table><tr><th>Symbol</th><th>Qty</th></tr>{pos_rows}</table>

  <h2>Trades ledger ({len(ledger)})</h2>
  <table><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit</th><th>P&L</th><th>Conf</th><th>Correct?</th><th>Signal cat</th></tr>
  {led_rows}</table>

  <h2>Signal tracker (self-learned evidence priors)</h2>
  <table><tr><th>Category</th><th>n</th><th>Win rate</th><th>Edge/trade</th></tr>{tr_rows}</table>

  <h2>Why my thinking changed</h2>
  <ul>{think_html}</ul>

  <h2>Run reports</h2>
  <div class="grid">{rep_cards}</div>

  <h2>📘 Living playbook (rewritten after every close run)</h2>
  <div class="playbook">{playbook_html}</div>
</main>
<footer>⚠️ Paper trading only — no live orders. Research artifact, not investment advice.</footer>
</body></html>"""


if __name__ == "__main__":
    out = ROOT / "dashboard.html"
    out.write_text(build())
    print(f"wrote {out}")
