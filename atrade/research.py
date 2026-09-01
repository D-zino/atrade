"""Autonomous research layer.

Sources (best-effort; every failure is logged and skipped, never fatal):
  - Yahoo Finance v8 chart API: daily bars + reference prices (primary)
  - FRED CSV endpoints: bond yields, fed funds, unemployment, CPI, PPI
  - stooq.com daily CSV: fallback reference prices when Yahoo fails
  - RSS news feeds (Google News topic queries)
  - SEC EDGAR data.sec.gov: recent filings incl. Form 4 insider activity
  - Frankfurter (ECB) FX rates: USD/EUR/GBP/JPY
  - research/inbox.json: qualitative notes injected by the human/AI researcher

Output: research/summary_{date}.json with normalized "notes", "prices", "bars".
"""
from __future__ import annotations

import csv
import io
import json
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

from . import util

UA = "Mozilla/5.0 (X11; Linux x86_64) TradingResearchAgent/1.0 (paper-trading research; educational)"
SEC_UA = "TradingResearchAgent/1.0 (educational paper-trading research; contact: admin@example.com)"

# SEC CIK lookup for universe tickers (used by data.sec.gov endpoints)
CIK_MAP = {
    "AAPL": "0000320193", "MSFT": "0000789019", "NVDA": "0001045810",
    "TSLA": "0001318605", "AMD": "0000002488", "META": "0001326801",
    "AMZN": "0001018724", "GOOGL": "0001652044", "JPM": "0000019617",
    "XOM": "0000034088", "CVX": "0000093410", "SPY": "0001064642",
    "QQQ": "0001067837", "IWM": "0001064641",
}


def _fetch(url: str, timeout: int = 20, headers: dict | None = None) -> str | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        util.log(f"fetch failed {url[:90]}: {e}", "WARN")
        return None


# ---------------------------------------------------------------------------
# Yahoo Finance daily bars (primary price source)
# ---------------------------------------------------------------------------
def fetch_yahoo(symbols: list[str], range_str: str = "3mo", interval: str = "1d") -> dict:
    """Return {symbol: {'date','open','high','low','close','volume','prev_close','chg_pct'}}."""
    out = {}
    for sym in symbols:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(sym)}"
               f"?range={range_str}&interval={interval}&includePrePost=false")
        raw = _fetch(url, timeout=15)
        if not raw:
            continue
        try:
            data = json.loads(raw)
            res = (data.get("chart") or {}).get("result") or []
            if not res:
                continue
            r = res[0]
            ts = r.get("timestamp") or []
            q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
            opens, highs, lows, closes, vols = q.get("open"), q.get("high"), q.get("low"), q.get("close"), q.get("volume")
            rows = []
            for i, t in enumerate(ts):
                c = closes[i] if i < len(closes) else None
                if c is None:
                    continue
                rows.append({
                    "t": datetime.fromtimestamp(t).strftime("%Y-%m-%d"),
                    "o": opens[i] if i < len(opens) else c,
                    "h": highs[i] if i < len(highs) else c,
                    "l": lows[i] if i < len(lows) else c,
                    "c": c,
                    "v": vols[i] if i < len(vols) else 0,
                })
            if not rows:
                continue
            last = rows[-1]
            prev = rows[-2] if len(rows) > 1 else None
            out[sym] = {"symbol": sym, "date": last["t"], "open": last["o"], "high": last["h"],
                        "low": last["l"], "close": last["c"], "volume": last["v"],
                        "prev_close": prev["c"] if prev else last["c"],
                        "chg_pct": util.pct_change(last["c"], prev["c"]) if prev else None,
                        "bars": rows}
        except Exception as e:
            util.log(f"yahoo parse failed {sym}: {e}", "WARN")
        time.sleep(0.25)
    return out


# ---------------------------------------------------------------------------
# stooq fallback (JS-challenged in some sandboxes; kept for portability)
# ---------------------------------------------------------------------------
def fetch_stooq(symbols: list[str]) -> dict:
    out = {}
    for sym in symbols:
        code = sym.lower().replace("-", ".") + ".us" if not sym.lower().endswith((".f", ".us")) else sym.lower()
        raw = _fetch(f"https://stooq.com/q/d/l/?s={code}&i=d", timeout=12)
        if not raw:
            continue
        try:
            rows = list(csv.reader(io.StringIO(raw)))
            data = [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in rows[1:]
                    if len(r) >= 6 and r[1] not in ("", "None")]
            if not data:
                continue
            d, o, h, l, c = data[-1]
            prev = data[-2][4] if len(data) >= 2 else c
            out[sym] = {"symbol": sym, "date": d, "open": o, "high": h, "low": l,
                        "close": c, "prev_close": prev, "chg_pct": util.pct_change(c, prev)}
        except Exception as e:
            util.log(f"stooq parse failed {sym}: {e}", "WARN")
    return out


# ---------------------------------------------------------------------------
# FRED macro series (cached once per day — daily series don't change intraday)
# ---------------------------------------------------------------------------
def fetch_fred(series: list[str], cache_dir: Path | None = None) -> dict:
    cache_file = (cache_dir or Path(".")) / "research" / "fred_cache.json"
    cached = {}
    if cache_file.exists():
        cached = util.read_json(cache_file, {}) or {}
    today = date.today().isoformat()
    out = {}
    for sid in series:
        if cached.get(sid, {}).get("_day") == today:
            out[sid] = cached[sid]
            continue
        raw = _fetch(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}", timeout=20)
        if not raw:
            # fall back to previous cached value (stale but better than nothing)
            if sid in cached:
                out[sid] = {**cached[sid], "_stale": True}
                util.log(f"FRED {sid} fetch failed; using cached value", "WARN")
            continue
        try:
            rows = list(csv.reader(io.StringIO(raw)))
            vals = [(r[0], r[1]) for r in rows[1:] if len(r) >= 2 and r[1] not in ("", ".")]
            if not vals:
                continue
            d, v = vals[-1]
            prev = vals[-2][1] if len(vals) >= 2 else None
            out[sid] = {"date": d, "value": float(v),
                        "prev": float(prev) if prev is not None else None,
                        "pct_chg": util.pct_change(float(v), float(prev)) if prev is not None else None,
                        "chg_units": (float(v) - float(prev)) if prev is not None else None,
                        "_day": today}
        except Exception as e:
            util.log(f"FRED parse failed for {sid}: {e}", "WARN")
    if cache_dir is not None:
        merged = dict(cached)
        merged.update(out)
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        util.write_json(cache_file, merged)
    return out


def fred_notes(fred: dict) -> list[dict]:
    notes = []
    def add(sid, title, direction, summary, strength):
        notes.append({"category": "macro", "tickers": [], "title": title, "summary": summary,
                      "direction": direction, "strength": strength, "source": "fred",
                      "fred_series": sid, "date": date.today().isoformat()})
    d10 = fred.get("DGS10")
    if d10 and d10.get("value") is not None:
        chg = d10.get("chg_units") or 0
        add("DGS10", "10Y Treasury yield", "bearish" if chg > 0 else "bullish",
            f"10Y yield {d10['value']:.2f}% ({'+' if chg > 0 else ''}{chg:.2f}pt vs prior). "
            f"Rising yields pressure long-duration growth stocks; falling yields favor them.",
            min(0.7, 0.5 + abs(chg) * 2))
    d2 = fred.get("DGS2")
    if d2 and d2.get("value") is not None:
        chg = d2.get("chg_units") or 0
        add("DGS2", "2Y Treasury yield / Fed expectations", "bearish" if chg > 0 else "bullish",
            f"2Y yield {d2['value']:.2f}% ({'+' if chg > 0 else ''}{chg:.2f}pt). Tracks front-end rate expectations.",
            min(0.6, 0.5 + abs(chg) * 2))
    ff = fred.get("FEDFUNDS")
    if ff and ff.get("value") is not None:
        add("FEDFUNDS", "Fed funds rate", "neutral",
            f"Fed funds at {ff['value']:.2f}%. Level only — watch FOMC speakers for direction.", 0.3)
    un = fred.get("UNRATE")
    if un and un.get("value") is not None:
        chg = un.get("chg_units") or 0
        add("UNRATE", "Unemployment rate", "bullish" if chg < 0 else "bearish",
            f"Unemployment {un['value']:.1f}% ({'+' if chg > 0 else ''}{chg:.1f}pt). "
            f"Cooling labor market raises easing odds.", min(0.65, 0.45 + abs(chg) * 3))
    cpi = fred.get("CPIAUCSL")
    if cpi and cpi.get("pct_chg") is not None:
        ann = cpi["pct_chg"] * 12 * 100
        add("CPIAUCSL", "CPI inflation momentum", "bullish" if ann < 3.0 else "bearish",
            f"CPI MoM annualized ≈ {ann:.1f}%. Disinflation → Fed easing → risk-on.", min(0.7, 0.4 + abs(3.0 - ann) * 0.08))
    ppi = fred.get("PPIFID")
    if ppi and ppi.get("pct_chg") is not None:
        ann = ppi["pct_chg"] * 12 * 100
        add("PPIFID", "PPI producer prices", "bullish" if ann < 3.0 else "bearish",
            f"PPI MoM annualized ≈ {ann:.1f}%. Producer inflation pressure gauge.", min(0.6, 0.4 + abs(3.0 - ann) * 0.06))
    return notes


# ---------------------------------------------------------------------------
# FX (Frankfurter / ECB)
# ---------------------------------------------------------------------------
def fetch_fx() -> list[dict]:
    try:
        raw = _fetch("https://api.frankfurter.app/latest?from=USD&to=EUR,GBP,JPY", timeout=15)
        if not raw:
            return []
        data = json.loads(raw)
        rates = data.get("rates") or {}
        notes = []
        eur = rates.get("EUR")
        if eur is not None:
            # EUR/USD up = dollar weak
            eurusd = 1.0 / eur
            notes.append({"category": "fx", "tickers": ["SPY", "QQQ"],
                          "title": f"EUR/USD {eurusd:.4f}",
                          "summary": f"EUR/USD ≈ {eurusd:.4f}. Dollar {'weak' if eurusd > 1.10 else 'firm'}; "
                                     f"{'tailwind for multinational earnings / commodities' if eurusd > 1.10 else 'mild headwind for multinationals'}.",
                          "direction": "bullish" if eurusd > 1.10 else "bearish",
                          "strength": 0.3, "source": "frankfurter", "date": data.get("date")})
        return notes
    except Exception as e:
        util.log(f"fx fetch failed: {e}", "WARN")
        return []


# ---------------------------------------------------------------------------
# RSS news
# ---------------------------------------------------------------------------
def fetch_rss_notes(queries: list[str], max_per_query: int = 4) -> list[dict]:
    notes = []
    for q in queries:
        url = ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q) +
               "+when:2d&hl=en-US&gl=US&ceid=US:en")
        raw = _fetch(url)
        if not raw:
            continue
        try:
            root = ET.fromstring(raw)
            for it in root.findall(".//item")[:max_per_query]:
                title = (it.findtext("title") or "").strip()
                pub = (it.findtext("pubDate") or "")
                src = it.find("source")
                source = src.text if src is not None and src.text else "rss"
                if title:
                    notes.append({"category": "news", "tickers": [], "title": title,
                                  "summary": title, "direction": "neutral",
                                  "strength": 0.35, "source": f"rss:{source}", "date": pub[:16],
                                  "query": q})
        except Exception as e:
            util.log(f"RSS parse failed for '{q}': {e}", "WARN")
        time.sleep(0.3)
    return notes


# ---------------------------------------------------------------------------
# SEC EDGAR (data.sec.gov submissions API — includes Form 4 insider activity)
# ---------------------------------------------------------------------------
def fetch_sec_filings(tickers: list[str], max_per: int = 4) -> list[dict]:
    out = []
    for t in tickers:
        cik = CIK_MAP.get(t.upper())
        if not cik:
            continue
        raw = _fetch(f"https://data.sec.gov/submissions/CIK{cik}.json", timeout=15,
                     headers={"User-Agent": SEC_UA})
        if not raw:
            continue
        try:
            d = json.loads(raw)
            rec = (d.get("filings") or {}).get("recent") or {}
            forms = rec.get("form") or []
            dates = rec.get("filingDate") or []
            descs = rec.get("primaryDocDescription") or []
            for i in range(min(max_per, len(forms))):
                f = (forms[i] or "").upper()
                if f in ("4", "13D", "13G", "8-K", "10-Q", "10-K"):
                    desc = descs[i] if i < len(descs) else ""
                    cat = "insider" if f == "4" else ("analyst" if f in ("13D", "13G") else "sec_filings")
                    out.append({"ticker": t, "form": f, "date": dates[i] if i < len(dates) else "",
                                "desc": (desc or "")[:80], "category": cat, "source": "sec-edgar"})
        except Exception as e:
            util.log(f"SEC parse failed {t}: {e}", "WARN")
        time.sleep(0.25)
    return out


def sec_notes(filings: list[dict]) -> list[dict]:
    notes = []
    for f in filings:
        ticker = f.get("ticker")
        form = f.get("form", "")
        if form == "4":
            notes.append({"category": "insider", "tickers": [ticker],
                          "title": f"{ticker} Form 4 insider transaction",
                          "summary": f"{ticker} reported insider ownership change (Form 4, filed {f.get('date')}). "
                                     f"{f.get('desc') or ''}. Track direction at the filing XML level or via inbox research.",
                          "direction": "neutral", "strength": 0.35, "source": "sec-edgar",
                          "date": f.get("date"), "sec_form": "4"})
        elif form in ("13D", "13G"):
            notes.append({"category": "analyst", "tickers": [ticker],
                          "title": f"{ticker} {form} beneficial-ownership filing",
                          "summary": f"{ticker}: {form} filing on {f.get('date')} — activist/institutional stake change.",
                          "direction": "neutral", "strength": 0.4, "source": "sec-edgar",
                          "date": f.get("date"), "sec_form": form})
        elif form in ("8-K", "10-Q", "10-K"):
            notes.append({"category": "sec_filings", "tickers": [ticker],
                          "title": f"{ticker} filed {form} ({f.get('date')})",
                          "summary": f"{ticker}: {form} filed {f.get('date')}. {f.get('desc') or ''}",
                          "direction": "neutral", "strength": 0.3, "source": "sec-edgar",
                          "date": f.get("date"), "sec_form": form})
    return notes


# ---------------------------------------------------------------------------
# Research inbox (agent-injected qualitative notes)
# ---------------------------------------------------------------------------
def load_inbox(state_dir: Path, pkg_research_dir: Path) -> list[dict]:
    notes = []
    for p in (state_dir / "inbox.json", pkg_research_dir / "inbox.json"):
        if p.exists():
            data = util.read_json(p, [])
            if isinstance(data, dict):
                data = data.get("notes", [])
            if isinstance(data, list):
                notes += data
    # dedupe by (title, summary)
    seen, out = set(), []
    for n in notes:
        key = (n.get("title"), n.get("summary"))
        if key not in seen:
            seen.add(key)
            out.append(n)
    return out


def clear_inbox(state_dir: Path) -> None:
    f = state_dir / "inbox.json"
    if f.exists():
        util.write_json(f, {"notes": [], "cleared": util.utc_iso()})


# ---------------------------------------------------------------------------
# Notes from prices (benchmarks + commodities)
# ---------------------------------------------------------------------------
def price_notes(prices: dict, universe: list[str]) -> list[dict]:
    notes = []
    bench_map = {"SPY": ("S&P 500", "broad market"), "QQQ": ("Nasdaq-100", "growth/tech"),
                 "IWM": ("Russell 2000", "small caps / risk appetite")}
    for sym, p in prices.items():
        chg = p.get("chg_pct")
        if chg is None:
            continue
        if sym in bench_map:
            name, tag = bench_map[sym]
            notes.append({"category": "sector", "tickers": [sym], "title": f"{name} moved {chg*100:+.1f}%",
                          "summary": f"{name} closed {chg*100:+.2f}% to {p['close']:.2f}; {tag} regime gauge.",
                          "direction": "bullish" if chg > 0.003 else ("bearish" if chg < -0.003 else "neutral"),
                          "strength": min(0.8, 0.5 + abs(chg) * 6), "source": "yahoo",
                          "date": p["date"]})
        elif sym == "GC=F":
            notes.append({"category": "commodities", "tickers": ["GLD"],
                          "title": f"Gold {chg*100:+.1f}%", "direction": "bullish" if chg > 0.004 else "bearish",
                          "summary": f"Gold {chg*100:+.2f}% to ${p['close']:.0f}. "
                                     f"{'Safe-haven bid / inflation hedge.' if chg > 0 else 'Risk appetite.'}",
                          "strength": min(0.75, 0.45 + abs(chg) * 8), "source": "yahoo", "date": p["date"]})
        elif sym == "CL=F":
            notes.append({"category": "commodities", "tickers": ["USO", "XLE"],
                          "title": f"WTI crude {chg*100:+.1f}%", "direction": "bullish" if chg > 0.004 else "bearish",
                          "summary": f"WTI {chg*100:+.2f}% to ${p['close']:.2f}. "
                                     f"{'Supply risk / demand strength.' if chg > 0 else 'Demand fear / easing supply.'}",
                          "strength": min(0.75, 0.45 + abs(chg) * 8), "source": "yahoo", "date": p["date"]})
    return notes


# ---------------------------------------------------------------------------
# Master research pass
# ---------------------------------------------------------------------------
def run_research(cfg: dict, state_dir: Path, universe: list[str]) -> dict:
    util.log("Research pass starting...")
    notes: list[dict] = []
    sources = {"fred": 0, "prices": 0, "rss": 0, "sec": 0, "fx": 0, "inbox": 0}

    # 1) FRED macro (cached once per day)
    fred = fetch_fred(cfg.get("fred_series", []), cache_dir=state_dir)
    fnotes = fred_notes(fred)
    notes += fnotes
    sources["fred"] = len(fnotes)
    util.log(f"FRED: {len(fred)} series -> {len(fnotes)} notes")

    # 2) Prices: Yahoo primary, stooq fallback for any misses
    price_universe = list(universe) + ["GC=F", "CL=F"]
    prices = fetch_yahoo(price_universe, range_str="3mo")
    if len(prices) < len(price_universe) * 0.5:
        util.log("Yahoo coverage thin — trying stooq fallback", "WARN")
        stooq = fetch_stooq(price_universe)
        for sym, p in stooq.items():
            prices.setdefault(sym, p)
    sources["prices"] = len(prices)
    util.log(f"prices: {len(prices)}/{len(price_universe)} symbols (yahoo+stooq)")
    notes += price_notes(prices, universe)
    sources["prices"] += 4  # commodity/benchmark notes

    # 3) FX
    fx = fetch_fx()
    notes += fx
    sources["fx"] = len(fx)

    # 4) RSS news
    rss = fetch_rss_notes(cfg.get("rss_queries", []))
    notes += rss
    sources["rss"] = len(rss)
    util.log(f"RSS: {len(rss)} items")

    # 5) SEC EDGAR
    if cfg.get("sec_enabled"):
        filings = fetch_sec_filings(cfg.get("universe", [])[:8])
        sn = sec_notes(filings)
        notes += sn
        sources["sec"] = len(sn)
        util.log(f"SEC: {len(filings)} filings -> {len(sn)} notes")

    # 6) inbox (agent research)
    from atrade import config as config_mod
    inbox = load_inbox(state_dir, config_mod.PACKAGE_DIR / "research")
    notes += inbox
    sources["inbox"] = len(inbox)
    util.log(f"inbox: {len(inbox)} notes")

    # normalize: strip 'bars' from price dicts stored in summary (keep compact)
    compact_prices = {k: {kk: vv for kk, vv in v.items() if kk != "bars"} for k, v in prices.items()}
    summary = {
        "asof": util.utc_iso(),
        "sources": sources,
        "notes": notes,
        "prices": compact_prices,
        "bars": {k: v.get("bars", []) for k, v in prices.items() if v.get("bars")},
        "fred": fred,
    }
    fname = state_dir / "research" / f"summary_{date.today().isoformat()}.json"
    util.write_json(fname, summary)
    util.write_json(state_dir / "research" / "latest.json", summary)
    util.log(f"Research pass done: {len(notes)} notes -> {fname.name}")
    return summary


def extract_price_map(summary: dict, universe: list[str]) -> dict:
    p = summary.get("prices") or {}
    return {sym: float(v["close"]) for sym, v in p.items() if v and v.get("close")}
