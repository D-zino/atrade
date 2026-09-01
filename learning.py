"""Self-improvement loop.

After each close run we:
  1. Grade every completed trade: was the hypothesis right? (label 1/0)
  2. Attribute outcomes to evidence categories (which mattered vs noise)
  3. Update the playbook signal tracker: per-category win rate / edge / n
  4. Discover / decay indicator relationships (small empirical table)
  5. Rewrite PLAYBOOK.md with new lessons, rules, and an updated signal tracker
  6. Record per-run "thinking changes" so the reports can show evolution

The playbook is the agent's memory of what actually worked — it is consulted
as priors when building tomorrow's hypotheses (signals.py), closing the loop.
"""
from __future__ import annotations

from datetime import date

from . import market, util


def grade_trades(closed: list[dict]) -> list[dict]:
    """Label each closed trade: hypothesis_correct (1/0) + lesson line."""
    for t in closed:
        pnl = t.get("pnl") or 0.0
        hyp = t.get("hypothesis") or {}
        conf = hyp.get("confidence") or 0.5
        t["hypothesis_correct"] = 1 if pnl > 0 else 0
        # magnitude context: did it beat what a coin flip at that confidence implies?
        expect = conf - 0.5  # expected edge
        t["vs_expected"] = round(pnl, 2)
        t["lesson"] = (f"{t['symbol']} {t['side']} {pnl:+.2f} ({t.get('pnl_pct', 0)*100:+.2f}%) "
                       f"conf={conf:.2f} → {'CONFIRMED' if pnl > 0 else 'REFUTED'} hypothesis "
                       f"({hyp.get('dominant_category', '?')} evidence)")
    return closed


def _cat_from_trade(t: dict) -> str:
    hyp = t.get("hypothesis") or {}
    return hyp.get("dominant_category") or "other"


def update_signal_tracker(tracker: dict, closed: list[dict], decay: float = 0.92) -> dict:
    """Per-category win-rate tracker with exponential decay (recency-weighted)."""
    import copy
    tr = copy.deepcopy(tracker) or {}
    for t in closed:
        cat = _cat_from_trade(t)
        st = tr.setdefault(cat, {"n": 0, "wins": 0, "decay_n": 0.0, "decay_wins": 0.0,
                                 "edge_sum": 0.0, "tickers": [], "last": None})
        win = t.get("hypothesis_correct", 0)
        st["n"] += 1
        st["wins"] += win
        st["decay_n"] = st["decay_n"] * decay + 1.0
        st["decay_wins"] = st["decay_wins"] * decay + win
        st["edge_sum"] += (t.get("pnl") or 0.0)
        tkr = t.get("symbol")
        if tkr not in st["tickers"]:
            st["tickers"].append(tkr)
        st["last"] = t.get("closed_at")
    for cat, st in tr.items():
        if cat.startswith("_") or not isinstance(st, dict):
            continue
        st["win_rate"] = round(st["wins"] / st["n"], 3) if st["n"] else None
        st["decay_win_rate"] = round(st["decay_wins"] / st["decay_n"], 3) if st["decay_n"] else None
        st["edge"] = round(st["edge_sum"] / st["n"], 2) if st["n"] else None
        st["priority"] = round(st["win_rate"] - 0.5 + (st["decay_win_rate"] - 0.5), 3) if st["win_rate"] is not None else 0.0
    return tr


def discover_indicators(closed: list[dict], tracker: dict) -> list[dict]:
    """Crude empirical indicator discovery: correlate trade-side sign with outcome
    for each evidence category present, and log any newly found relationships."""
    found = []
    agg = {}
    for t in closed:
        hyp = t.get("hypothesis") or {}
        for ev in hyp.get("evidence") or []:
            cat = ev.get("category")
            if not cat:
                continue
            d = agg.setdefault(cat, {"agree": 0, "contra": 0, "pnl_agree": 0.0, "pnl_contra": 0.0})
            side = 1 if t.get("side") == "long" else -1
            ev_dir = ev.get("direction", 0)
            agrees = (side * ev_dir) > 0
            pnl = t.get("pnl") or 0.0
            if agrees:
                d["agree"] += 1
                d["pnl_agree"] += pnl
            elif ev_dir != 0:
                d["contra"] += 1
                d["pnl_contra"] += pnl
    for cat, d in agg.items():
        total = d["agree"] + d["contra"]
        if total < 2:
            continue
        hit = d["pnl_agree"] / max(1, d["agree"]) if d["agree"] else 0.0
        miss = d["pnl_contra"] / max(1, d["contra"]) if d["contra"] else 0.0
        found.append({"category": cat, "agree_n": d["agree"], "contra_n": d["contra"],
                      "avg_pnl_when_agree": round(hit, 2), "avg_pnl_when_contra": round(miss, 2),
                      "relationship": f"evidence direction agreement favored "
                                      f"{'agreement' if hit > miss else 'contradiction'} by {abs(hit-miss):.2f}/trade"})
    # store discoveries in tracker so reports can cite them
    if found:
        tracker["_discoveries"] = found
    return found


def generate_lessons(closed: list[dict], tracker: dict, run_summary: dict) -> list[str]:
    """Plain-English lessons from this run's completed trades."""
    lessons = []
    wins = [t for t in closed if (t.get("pnl") or 0) > 0]
    losses = [t for t in closed if (t.get("pnl") or 0) <= 0]
    if wins:
        cats = {}
        for t in wins:
            c = _cat_from_trade(t)
            cats[c] = cats.get(c, 0) + 1
        top = max(cats, key=cats.get)
        lessons.append(f"Winning trades clustered around '{top}' evidence "
                       f"({cats[top]} of {len(wins)} winners) — this signal category is earning its prior.")
    if losses:
        cats = {}
        for t in losses:
            c = _cat_from_trade(t)
            cats[c] = cats.get(c, 0) + 1
        top = max(cats, key=cats.get)
        lessons.append(f"Losers were dominated by '{top}' ({cats[top]} of {len(losses)}) — "
                       f"reducing prior weight for that category until it demonstrates edge.")
    for t in closed:
        move = t.get("pnl_pct") or 0
        if abs(move) >= 0.01:
            hyp = t.get("hypothesis") or {}
            conf = hyp.get("confidence") or 0.5
            lessons.append(f"{t['symbol']}: {t['side']} move of {move*100:+.2f}% "
                           f"({'vindicated' if move > 0 else 'against'}) a {conf:.0%}-confidence thesis. "
                           f"Falsifier used: {(hyp.get('falsifiers') or ['n/a'])[0]}")
    # global edge
    cats_above = [c for c, s in tracker.items() if not c.startswith("_") and s.get("win_rate") and s["win_rate"] > 0.55]
    cats_below = [c for c, s in tracker.items() if not c.startswith("_") and s.get("win_rate") and s["win_rate"] < 0.40]
    if cats_above:
        lessons.append(f"Signal tracker now favors: {', '.join(cats_above)} (win rate > 55%).")
    if cats_below:
        lessons.append(f"Signal tracker now discounts: {', '.join(cats_below)} (win rate < 40%).")
    if not closed:
        lessons.append("No trades closed this run (no day trades were open) — nothing to grade yet.")
    return lessons


def playbook_body(tracker: dict, lessons: list[str], closed: list[dict],
                  state: dict, cfg: dict) -> str:
    """Render the full PLAYBOOK.md from current state."""
    lines = []
    lines.append("# 📘 A-Trade Playbook — living trading rules & signal tracker")
    lines.append("")
    lines.append(f"*Last updated: {util.utc_iso()}*")
    lines.append("")
    lines.append("This file is rewritten after every close run by the self-improvement loop. "
                 "It is **not** a fixed rulebook — rules here are provisional hypotheses "
                 "about what the market rewards, updated from evidence.")
    lines.append("")
    lines.append("## How the loop works")
    lines.append("1. Research → hypotheses with confidence (priors from this tracker).")
    lines.append("2. Trade day (paper only).")
    lines.append("3. Grade: was each hypothesis right? Which evidence mattered vs noise?")
    lines.append("4. Update tracker → rewrite this file → next day's priors change.")
    lines.append("")
    lines.append("## Current signal tracker (recency-weighted win rates)")
    lines.append("")
    lines.append("| Signal category | n | Wins | Win rate | Recency w.r. | Edge $/trade | Priority | Trust |")
    lines.append("|---|---|---|---|---|---|---|---|")
    cats = sorted([c for c in tracker if not c.startswith("_")],
                  key=lambda c: -(tracker[c].get("win_rate") or 0))
    for c in cats:
        st = tracker[c]
        wr = util.fmt_pct(st.get("win_rate")) if st.get("win_rate") is not None else "n/a"
        dwr = util.fmt_pct(st.get("decay_win_rate")) if st.get("decay_win_rate") is not None else "n/a"
        edge = f"{st['edge']:+.2f}" if st.get("edge") is not None else "n/a"
        pr = f"{st.get('priority', 0):+.2f}"
        n = st.get("n") or 0
        trust = "HIGH" if n >= 15 else ("MED" if n >= 6 else "LOW")
        lines.append(f"| {c} | {n} | {st['wins']} | {wr} | {dwr} | {edge} | {pr} | {trust} |")
    lines.append("")
    lines.append("**Trust levels** (anti-overfitting rule): **LOW** (<6 samples — treated as noise, "
                 "prior heavily shrunk), **MED** (6–14 — partial weight), **HIGH** (15+ — full prior). "
                 "Do not trust a category until it reaches HIGH.")
    lines.append("")
    lines.append("## Rules in force (evolving)")
    lines.append("")
    for i, rule in enumerate(cfg.get("active_rules", _default_rules())):
        lines.append(f"{i+1}. {rule}")
    lines.append("")
    lines.append("## Lessons learned this run")
    lines.append("")
    if lessons:
        for l in lessons:
            lines.append(f"- {l}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## What mattered vs what was noise (evidence attribution)")
    lines.append("")
    disc = tracker.get("_discoveries") or []
    if disc:
        for d in disc:
            lines.append(f"- **{d['category']}**: when evidence direction agreed with the trade, "
                         f"avg P&L {d['avg_pnl_when_agree']:+.2f} vs {d['avg_pnl_when_contra']:+.2f} when it "
                         f"contradicted ({d['agree_n']} agree / {d['contra_n']} contra). → {d['relationship']}.")
    else:
        lines.append("- Not enough graded trades yet to attribute signal-vs-noise reliably.")
    lines.append("")
    lines.append("## Discovered indicators / relationships")
    lines.append("")
    inds = (state.get("discovered_indicators") or [])
    if inds:
        for i in inds[-12:]:
            lines.append(f"- {i.get('note')}")
    else:
        lines.append("- None validated yet. Candidate watchlist: pre-open gaps, RSI extremes, "
                     "yield-momentum alignment, options-flow imbalance (pending inbox data).")
    lines.append("")
    lines.append("## Open questions & falsifiable predictions")
    lines.append("")
    lines.append("- Does 'macro' evidence (yields/CPI) beat 'news' noise in day trades? Tracker will tell us.")
    lines.append("- Are high-confidence (≥0.75) setups worth the bigger size? Check win rate vs conf bucket.")
    lines.append("- Short trades: do they lose more often in a liquidity-rich tape? Track separately.")
    lines.append("")
    lines.append("---")
    lines.append("⚠️ Paper trading only. No live orders are ever placed. This playbook is a "
                 "research artifact, not investment advice.")
    return "\n".join(lines)


def _default_rules() -> list[str]:
    return [
        "Only trade hypotheses with confidence ≥ 60% (configurable).",
        "Day trades only: every position opened at the open run is flattened at the close run.",
        "Max 2 concurrent positions; max ~24% of equity deployed.",
        "Intraday stop: ~1.4% adverse move → defensive close (stop_hit recorded).",
        "Sizing scales with confidence: 60% → 6%, 70% → 9%, 80%+ → 12% of equity.",
        "Pause trading after 6 consecutive runs with no composite-score improvement, "
        "or 3 consecutive failed measurements.",
        "Benchmark = SPY buy & hold from the frozen baseline capture.",
    ]


def run_learning(closed: list[dict], tracker: dict, state: dict, cfg: dict,
                 research: dict | None = None) -> dict:
    """Full self-improvement pass. Returns updated tracker + lessons."""
    graded = grade_trades(closed)
    tracker = update_signal_tracker(tracker, graded)
    disc = discover_indicators(graded, tracker)
    state.setdefault("discovered_indicators", [])
    for d in disc:
        state["discovered_indicators"].append({
            "note": f"{d['category']}: {d['relationship']} (n={d['agree_n']+d['contra_n']})",
            "at": util.utc_iso(),
        })
    lessons = generate_lessons(graded, tracker, research or {})
    # prune tracker metadata that shouldn't persist long-term
    for c in list(tracker):
        if c.startswith("_") and c != "_discoveries":
            del tracker[c]
    return {"tracker": tracker, "lessons": lessons, "discoveries": disc, "graded": graded}
