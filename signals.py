"""Signal engine: research notes + technicals + playbook priors -> hypotheses.

A hypothesis is a tradable idea with:
  - symbol, side (long/short), confidence [0..1]
  - thesis (plain English)
  - supporting evidence (the notes that fed it)
  - falsifiers: "what new information would change my thesis"
  - priors pulled from the playbook's signal tracker (learning loop feedback)

Confidence model (all in [0,1]):
  base   = 0.50
  prior  = playbook signal-tracker edge for the dominant note category
  evid   = evidence-strength term from the note strengths
  agree  = directional agreement term (bull vs bear balance)
  fresh  = freshness decay (notes age)
  tech   = technical confluence adjustment (capped)
  conf   = base + 0.35*prior + 0.30*evid + 0.15*agree + 0.10*fresh + tech
Only hypotheses with conf >= min_confidence (default 0.60) may be traded.
"""
from __future__ import annotations

from datetime import date, datetime

from . import util

CATEGORY_BIAS = {
    "earnings": 0.08, "sec_filings": 0.04, "analyst": 0.05, "fed": 0.09,
    "macro": 0.06, "rates": 0.06, "geopolitics": 0.05, "commodities": 0.06,
    "fx": 0.04, "bonds": 0.05, "insider": 0.05, "options_flow": 0.05,
    "sector": 0.05, "tech": 0.04, "sentiment": 0.03, "news": 0.03,
    "historical_analog": 0.04, "other": 0.02,
}
# Falsifier templates per category — "what would change my thesis"
FALSIFIERS = {
    "earnings": "actual earnings/guidance misses consensus; post-earnings drift reverses",
    "analyst": "rating downgraded or price target cut below current price",
    "fed": "Fed signals a hike/hawkish surprise; dot plot shifts higher",
    "macro": "headline data prints strongly against the implied direction",
    "commodities": "underlying commodity reverses its move by more than 1%",
    "rates": "yields reverse direction by more than ~8-10 bps",
    "geopolitics": "de-escalation (if bullish risk) or escalation (if bearish)",
    "sector": "sector ETF breaks the day's range against the thesis",
    "sentiment": "retail sentiment flips against thesis; funding/crowding extremes",
    "insider": "offsetting insider sale/buy in the same name",
    "news": "headline is retracted or materially contradicted",
    "options_flow": "unusual flow reverses / block prints against thesis",
    "technical": "price breaks key intraday level (SMA20 / day range) against thesis",
    "sec_filings": "filing is amended/withdrawn or revealed as routine",
    "other": "any materially contradicting new information",
}


def _note_date(note: dict) -> date | None:
    d = note.get("date")
    if not d:
        return None
    try:
        return datetime.fromisoformat(str(d)[:10]).date()
    except Exception:
        try:
            return datetime.strptime(str(d)[:16], "%a, %d %b %Y").date()
        except Exception:
            return None


def _freshness(note: dict, today: date | None = None) -> float:
    today = today or date.today()
    nd = _note_date(note)
    if nd is None:
        return 0.7
    age = (today - nd).days
    return max(0.1, 0.9 ** age)


def _note_direction(note: dict) -> float:
    d = (note.get("direction") or "neutral").lower()
    if d in ("bullish", "bull", "positive", "buy", "upgrade", "long"):
        return 1.0
    if d in ("bearish", "bear", "negative", "sell", "downgrade", "short"):
        return -1.0
    return 0.0


def _note_strength(note: dict) -> float:
    s = note.get("strength")
    try:
        return max(0.0, min(1.0, float(s)))
    except (TypeError, ValueError):
        return 0.35


def build_hypotheses(research: dict, tech: dict[str, dict], tracker: dict,
                     universe: list[str], cfg: dict, today: date | None = None) -> list[dict]:
    """research: run_research() output. tracker: playbook signal tracker."""
    today = today or date.today()
    notes = research.get("notes", [])
    tech_notes = []
    for sym in universe:
        t = tech.get(sym)
        if t and t.get("chg_pct") is not None:
            tech_notes.append({"category": "technical", "tickers": [sym],
                               "title": f"{sym} daily move {t['chg_pct']*100:+.1f}%",
                               "summary": f"{sym} {t['chg_pct']*100:+.2f}% intraday/most-recent session",
                               "direction": "bullish" if t["chg_pct"] > 0.004 else
                                            ("bearish" if t["chg_pct"] < -0.004 else "neutral"),
                               "strength": min(0.6, 0.3 + abs(t["chg_pct"]) * 8),
                               "source": "bars"})
    all_notes = notes + tech_notes

    hyps = {}
    for note in all_notes:
        cat = note.get("category") or "other"
        cat = cat.lower()
        if cat in ("news", "other") and not note.get("tickers"):
            continue  # generic headlines rarely name a tradable
        tickers = [t for t in (note.get("tickers") or []) if t in universe]
        if not tickers:
            # macro/news with no ticker: apply to broad benchmarks
            tickers = ["SPY", "QQQ"] if note.get("direction") in ("bullish", "bearish") else []
        if not tickers:
            continue
        strength = _note_strength(note)
        dirn = _note_direction(note)
        if dirn == 0 and strength < 0.45:
            continue
        for sym in tickers:
            h = hyps.setdefault(sym, {"symbol": sym, "evidence": [], "bull": 0.0, "bear": 0.0,
                                      "cats": {}, "notes": []})
            h["bull"] += strength if dirn > 0 else 0
            h["bear"] += strength if dirn < 0 else 0
            h["cats"][cat] = h["cats"].get(cat, 0.0) + strength
            h["evidence"].append({"category": cat, "title": note.get("title"),
                                  "summary": note.get("summary"), "direction": dirn,
                                  "strength": strength, "source": note.get("source"),
                                  "fresh": round(_freshness(note, today), 2)})
            h["notes"].append(note)

    out = []
    for sym, h in hyps.items():
        bull, bear = h["bull"], h["bear"]
        net = bull - bear
        total = bull + bear
        if total < 0.45:  # too little evidence
            continue
        side = "long" if net >= 0 else "short"
        if side == "short" and sym in ("SPY", "QQQ", "IWM") and bull > 0 and bear == 0:
            pass  # allow shorting benchmarks only with real bearish evidence
        agree = abs(bull - bear) / total
        # playbook prior for the dominant category — with small-sample SHRINKAGE:
        # a win rate from 1-2 trades must not move confidence much; the prior
        # only approaches full strength as n grows (Bayesian-style pull toward 0).
        dom_cat = max(h["cats"], key=h["cats"].get)
        cat_stat = (tracker or {}).get(dom_cat, {})
        win_rate = cat_stat.get("win_rate")
        n = cat_stat.get("n") or 0
        shrink = n / (n + 5.0)  # n=0→0.0, n=5→0.5, n=20→0.8, n=45→0.9
        prior = (win_rate - 0.5) * 0.7 * shrink if isinstance(win_rate, (int, float)) else 0.0
        # per-symbol prior (Tier 2): symbols with proven edge get a small boost;
        # symbols with persistent negative edge get discounted (shrink-protected)
        sym_stat = ((tracker or {}).get("_symbols") or {}).get(sym, {})
        sym_wr = sym_stat.get("win_rate")
        sym_n = sym_stat.get("n") or 0
        if isinstance(sym_wr, (int, float)) and sym_n >= 4:
            sym_shrink = sym_n / (sym_n + 6.0)
            prior += (sym_wr - 0.5) * 0.5 * sym_shrink
        # sector-rotation bias (Tier 2): symbols in today's top sectors get a bump
        rot_top = ((research or {}).get("dynamic") or {}).get("sector_top") or []
        rot_etfs = {s["etf"] for s in rot_top}
        sector_of = cfg.get("sector_of") or {}
        sym_sector = sector_of.get(sym)
        if sym_sector and sym_sector in rot_etfs:
            prior += 0.03
        evid = (sum(e["strength"] for e in h["evidence"]) / len(h["evidence"]) - 0.5)
        fresh = sum(e["fresh"] for e in h["evidence"]) / len(h["evidence"])
        # technical confluence
        t = tech.get(sym) or {}
        tech_adj = 0.0
        if t.get("rsi14") is not None:
            if side == "long" and t["rsi14"] > 70:
                tech_adj -= 0.10
            if side == "short" and t["rsi14"] < 30:
                tech_adj -= 0.10
            if side == "long" and 40 <= t["rsi14"] <= 60:
                tech_adj += 0.04
        if t.get("above_sma20") and side == "long":
            tech_adj += 0.03
        if t.get("above_sma20") is False and side == "short":
            tech_adj += 0.03
        if t.get("mom5") is not None:
            tech_adj += 0.06 if (side == "long" and t["mom5"] > 0.02) else 0
            tech_adj -= 0.06 if (side == "long" and t["mom5"] < -0.02) else 0
        tech_adj = max(-0.12, min(0.12, tech_adj))

        conf = 0.50 + 0.35 * prior + 0.30 * evid + 0.15 * (agree - 0.5) * 2 + 0.10 * (fresh - 0.5) * 2 + tech_adj
        conf = max(0.0, min(0.97, conf))
        thesis, falsifiers = _thesis(sym, side, dom_cat, h, cat_stat)
        out.append({
            "symbol": sym, "side": side, "confidence": round(conf, 3),
            "thesis": thesis, "falsifiers": falsifiers,
            "dominant_category": dom_cat,
            "evidence": sorted(h["evidence"], key=lambda e: -e["strength"])[:8],
            "net_score": round(net, 2), "bull_strength": round(bull, 2),
            "bear_strength": round(bear, 2),
            "prior_used": round(prior, 3),
            "tradeable": conf >= cfg.get("min_confidence", 0.60),
        })
    out.sort(key=lambda x: -x["confidence"])
    return out


def _thesis(sym: str, side: str, dom_cat: str, h: dict, cat_stat: dict) -> tuple[str, list[str]]:
    top = h["evidence"][:3]
    bits = [f"{e['title']}" for e in top]
    prior_note = ""
    if isinstance(cat_stat.get("win_rate"), (int, float)):
        prior_note = (f" Playbook prior for '{dom_cat}' is "
                      f"{cat_stat['win_rate']*100:.0f}% win rate over {cat_stat.get('n', 0)} trades.")
    thesis = (f"Go {side} {sym} today: {'; '.join(bits)}. "
              f"Bull vs bear evidence: {h['bull']:.2f} vs {h['bear']:.2f}.{prior_note}")
    fals = [FALSIFIERS.get(dom_cat, FALSIFIERS["other"]),
            f"{sym} gaps/breaks against the thesis (more than ~0.8% adverse move)",
            "a stronger opposing catalyst (Fed/macro/earnings) prints during the day"]
    return thesis, fals


def rank_signals(hyps: list[dict]) -> list[dict]:
    """Tradeable hypotheses ranked; dedupe per symbol (keep highest confidence)."""
    best = {}
    for h in hyps:
        if not h.get("tradeable"):
            continue
        prev = best.get(h["symbol"])
        if prev is None or h["confidence"] > prev["confidence"]:
            best[h["symbol"]] = h
    return sorted(best.values(), key=lambda x: -x["confidence"])
