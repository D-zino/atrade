"""Run reports: plain-English markdown for open and close runs."""
from __future__ import annotations

from . import util


def report_header(run_type: str, mode: str) -> list[str]:
    ts = util.utc_iso()
    return [
        f"# A-Trade {run_type.upper()} run report — {ts}",
        "",
        f"*Mode: **{mode}** (paper/dry-run) | generated {ts}*",
        "",
    ]


def _portfolio_table(metrics: dict, comps: dict, score: float) -> list[str]:
    m = metrics
    return [
        "## Portfolio & performance",
        "",
        f"- **Total P&L (realized):** {util.fmt_usd(m.get('net_pnl'))}",
        f"- **Portfolio return:** {util.fmt_pct(m.get('portfolio_return'))}",
        f"- **Benchmark (SPY buy&hold from baseline):** {util.fmt_pct(m.get('benchmark_return'))}",
        f"- **Alpha vs SPY:** {util.fmt_pct(m.get('alpha'))}",
        f"- **Sharpe (realized, annualized):** {m.get('sharpe') if m.get('sharpe') is not None else 'n/a'}",
        f"- **Win rate:** {util.fmt_pct(m.get('win_rate'))} ({m.get('n_trades')} closed trades, {m.get('n_open')} open)",
        f"- **Composite score (35% ret / 30% alpha / 20% sharpe / 15% win):** **{score:.4f}** "
        f"(components: {comps})",
        "",
    ]


def _open_positions_table(positions: list[dict]) -> list[str]:
    if not positions:
        return ["## Open positions", "", "- none", ""]
    rows = ["## Open positions", "", "| Symbol | Qty | Avg entry | Last | Unrealized |", "|---|---|---|---|---|"]
    for p in positions:
        rows.append(f"| {p.get('symbol')} | {p.get('qty')} | {util.fmt_usd(float(p.get('avg_entry_price') or 0), 2)} | "
                    f"{util.fmt_usd(float(p.get('current_price') or 0), 2)} | "
                    f"{util.fmt_usd(float(p.get('unrealized_pl') or 0), 2)} |")
    rows.append("")
    return rows


def open_report(state: dict, research: dict, hyps: list[dict], opened: list[dict],
                skipped: list[str], tech: dict, mode: str) -> str:
    lines = report_header("open", mode)
    lines += _portfolio_table(state.get("last_metrics") or {},
                              state.get("last_components") or {},
                              state.get("last_score") or 0.0)
    # market-moving events
    lines += ["## Today's most important market-moving events", ""]
    notes = research.get("notes") or []
    strong = sorted([n for n in notes if (n.get("strength") or 0) >= 0.6],
                    key=lambda n: -(n.get("strength") or 0))[:8]
    if strong:
        for n in strong:
            lines.append(f"- **[{n.get('category')}]** {n.get('title')} — {n.get('summary')}")
    else:
        lines.append("- No high-strength catalysts detected in this research pass.")
    lines.append("")
    lines += _open_positions_table([])  # none at open (positions are opened below)
    lines += ["## New positions opened (hypotheses ≥ 60% confidence)", ""]
    if opened:
        lines.append("| Symbol | Side | Qty | Conf | Thesis | Falsifiers |")
        lines.append("|---|---|---|---|---|---|")
        for t in opened:
            h = t.get("hypothesis") or {}
            lines.append(f"| {t['symbol']} | {t['side']} | {t['qty']} | {h.get('confidence')*100:.0f}% | "
                         f"{h.get('thesis','')[:90]} | {'; '.join(h.get('falsifiers') or [])[:80]} |")
        lines.append("")
    else:
        lines.append("- No trades opened this session.")
    if skipped:
        lines += ["Skipped:", ""] + [f"- {s}" for s in skipped] + [""]
    lines += _active_hypotheses(hyps, max_n=8)
    lines += _thinking_section(state, opened)
    lines.append("---")
    lines.append("⚠️ Paper trading only — no live orders were placed.")
    return "\n".join(lines)


def _active_hypotheses(hyps: list[dict], max_n: int = 8) -> list[str]:
    lines = [f"## Active hypotheses (all, ranked by confidence)", ""]
    if not hyps:
        lines.append("- none")
        lines.append("")
        return lines
    lines.append("| Symbol | Side | Conf | Category | Thesis |")
    lines.append("|---|---|---|---|---|")
    for h in hyps[:max_n]:
        lines.append(f"| {h['symbol']} | {h['side']} | {h['confidence']*100:.0f}% | "
                     f"{h['dominant_category']} | {h['thesis'][:80]} |")
    lines.append("")
    return lines


def _thinking_section(state: dict, opened: list[dict]) -> list[str]:
    lines = ["## Why my thinking changed", ""]
    history = state.get("thinking_history") or []
    if history:
        lines.append(f"- Latest ({history[-1].get('date')}): {history[-1].get('change')}")
    if opened:
        t = opened[0]
        if isinstance(t, str):
            lines.append(f"- Today I acted on {t} (see open-run report for the hypothesis and falsifiers).")
        else:
            h = t.get("hypothesis") or {}
            lines.append(f"- Today I acted on {t['symbol']} ({h.get('confidence')*100:.0f}% conf) because "
                         f"{h.get('thesis','')[:140]}")
            lines.append(f"- This is falsified if: {'; '.join(h.get('falsifiers') or [])[:140]}")
    else:
        lines.append("- No new positions; existing hypotheses remain under observation.")
    lines.append("")
    return lines


def close_report(state: dict, research: dict, hyps: list[dict], closed: list[dict],
                 positions: list[dict], lessons: list[str], tracker: dict,
                 eval_res: dict, mode: str, opened_today: list[dict]) -> str:
    lines = report_header("close", mode)
    m = eval_res.get("metrics") or {}
    lines += _portfolio_table(m, eval_res.get("components") or {}, eval_res.get("score") or 0.0)
    lines += [f"- **Improvement vs best:** {'YES 🎉 (new best score)' if eval_res.get('improved') else 'no — '
              f'no-improve streak {eval_res.get("no_improve_streak")}/6'}",
              ""]
    if eval_res.get("pause"):
        lines += ["## ⏸ PAUSED", "",
                  "Pause condition triggered (6 runs without improvement, or 3 failed measurements). "
                  "Trading is halted until a human reviews and resumes. See state/pause.json.",
                  ""]
    lines += _open_positions_table(positions)
    lines += ["## Day trades closed today", ""]
    if closed:
        lines.append("| Symbol | Side | Qty | Entry | Exit | P&L | % | Conf | Hypothesis correct? |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for t in closed:
            h = t.get("hypothesis") or {}
            correct = "✅" if t.get("hypothesis_correct") else "❌"
            lines.append(f"| {t['symbol']} | {t['side']} | {t['qty']} | {util.fmt_usd(t.get('entry_price'),2)} | "
                         f"{util.fmt_usd(t.get('exit_price'),2)} | {util.fmt_usd(t.get('pnl'))} | "
                         f"{t.get('pnl_pct',0)*100:+.2f}% | {h.get('confidence',0)*100:.0f}% | {correct} |")
        lines.append("")
    else:
        lines.append("- No trades closed (nothing was open).")
        lines.append("")
    lines += ["## Lessons learned (self-improvement loop)", ""]
    if lessons:
        for l in lessons:
            lines.append(f"- {l}")
    else:
        lines.append("- none")
    lines.append("")
    lines += ["## Evidence that mattered vs noise", ""]
    disc = tracker.get("_discoveries") or []
    if disc:
        for d in disc:
            lines.append(f"- **{d['category']}**: agreement with trade direction averaged "
                         f"{d['avg_pnl_when_agree']:+.2f} vs {d['avg_pnl_when_contra']:+.2f} on contradiction "
                         f"({d['agree_n']}/{d['contra_n']} samples).")
    else:
        lines.append("- Still accumulating graded trades before attribution is meaningful.")
    lines.append("")
    lines += ["## Predictions for upcoming moves", ""]
    for h in hyps[:5]:
        lines.append(f"- {h['symbol']} {h['side']}: {h['thesis'][:110]} (conf {h['confidence']*100:.0f}%)")
    lines.append("")
    lines += ["## Open positions carrying overnight", ""]
    if positions:
        for p in positions:
            lines.append(f"- {p.get('symbol')} {p.get('qty')} shares — opened this session; will close at next close run.")
    else:
        lines.append("- none (all flat)")
    lines.append("")
    lines += _thinking_section(state, opened_today)
    lines += ["## Signal tracker snapshot", ""]
    lines.append("| Category | n | Win rate | Recency w.r. | Edge $ | Trust |")
    lines.append("|---|---|---|---|---|---|")
    for c in sorted([c for c in tracker if not c.startswith("_")],
                    key=lambda c: -(tracker[c].get("win_rate") or 0)):
        st = tracker[c]
        n = st.get("n") or 0
        trust = "HIGH" if n >= 15 else ("MED" if n >= 6 else "LOW")
        lines.append(f"| {c} | {n} | {util.fmt_pct(st.get('win_rate'))} | "
                     f"{util.fmt_pct(st.get('decay_win_rate'))} | "
                     f"{st.get('edge') if st.get('edge') is not None else 'n/a'} | {trust} |")
    lines.append("")
    lines.append("*Trust: LOW (<6 samples — prior shrunk), MED (6–14), HIGH (15+). "
                 "Sample sizes must grow before a category's prior is trusted.*")
    lines.append("")
    lines.append("---")
    lines.append("⚠️ Paper trading only — no live orders were placed. "
                 "Benchmarks (SPY/QQQ) are tracked in the ledger/baseline.")
    return "\n".join(lines)
