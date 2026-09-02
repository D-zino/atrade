"""Technical indicators computed from OHLC bars (pure stdlib).

Used to add quantitative confluence to research-driven hypotheses:
RSI(14), SMA(20/50), 5-day momentum, volume ratio, gap vs prev close,
distance from 52-week high/low.
"""
from __future__ import annotations


def _closes(bars: list[dict]) -> list[float]:
    out = []
    for b in bars:
        try:
            v = float(b.get("c") or b.get("close") or 0)
            out.append(v)
        except (TypeError, ValueError):
            continue
    return out


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g = gains / period
    avg_l = losses / period
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (period - 1) + max(d, 0.0)) / period
        avg_l = (avg_l * (period - 1) + max(-d, 0.0)) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def momentum(closes: list[float], n: int = 5) -> float | None:
    if len(closes) <= n:
        return None
    base = closes[-n - 1]
    if base == 0:
        return None
    return closes[-1] / base - 1.0


def volume_ratio(bars: list[dict], n: int = 20) -> float | None:
    vols = []
    for b in bars:
        try:
            v = float(b.get("v") or b.get("volume") or 0)
            vols.append(v)
        except (TypeError, ValueError):
            continue
    if len(vols) < 2:
        return None
    avg = sum(vols[:-1][-n:]) / max(1, len(vols[:-1][-n:]))
    if avg <= 0:
        return None
    return vols[-1] / avg


def technical_snapshot(bars: list[dict]) -> dict | None:
    closes = _closes(bars)
    if len(closes) < 20:
        return None
    last = closes[-1]
    high52 = max(closes[-252:]) if len(closes) >= 252 else max(closes)
    low52 = min(closes[-252:]) if len(closes) >= 252 else min(closes)
    return {
        "close": last,
        "prev_close": closes[-2] if len(closes) > 1 else last,
        "chg_pct": (closes[-1] / closes[-2] - 1.0) if len(closes) > 1 else None,
        "rsi14": rsi(closes),
        "sma20": sma(closes, 20),
        "sma50": sma(closes, 50),
        "mom5": momentum(closes, 5),
        "vol_ratio": volume_ratio(bars),
        "near_52w_high": last / high52 - 1.0 if high52 else None,
        "near_52w_low": last / low52 - 1.0 if low52 else None,
        "above_sma20": (last > sma(closes, 20)) if sma(closes, 20) else None,
        "above_sma50": (last > sma(closes, 50)) if sma(closes, 50) else None,
    }


def technical_notes(tech: dict[str, dict], universe: list[str]) -> list[dict]:
    """Technical snapshots -> neutral-weight confluence notes."""
    notes = []
    for sym in universe:
        t = tech.get(sym)
        if not t:
            continue
        score = 0.0
        reasons = []
        if t.get("rsi14") is not None:
            if t["rsi14"] < 30:
                score += 0.5
                reasons.append(f"RSI {t['rsi14']:.0f} oversold")
            elif t["rsi14"] > 70:
                score -= 0.5
                reasons.append(f"RSI {t['rsi14']:.0f} overbought")
        if t.get("mom5") is not None:
            score += 0.4 if t["mom5"] > 0.02 else (-0.4 if t["mom5"] < -0.02 else 0)
            reasons.append(f"5d mom {t['mom5']*100:+.1f}%")
        if t.get("above_sma20"):
            score += 0.3
            reasons.append("above SMA20")
        elif t.get("above_sma20") is False:
            score -= 0.3
            reasons.append("below SMA20")
        if t.get("above_sma50"):
            score += 0.3
            reasons.append("above SMA50")
        elif t.get("above_sma50") is False:
            score -= 0.3
            reasons.append("below SMA50")
        if t.get("vol_ratio") is not None and t["vol_ratio"] > 1.5:
            score += 0.2
            reasons.append(f"volume {t['vol_ratio']:.1f}x")
        if abs(score) >= 0.3:
            direction = "bullish" if score > 0 else "bearish"
            notes.append({"category": "technical", "tickers": [sym],
                          "title": f"{sym} technical {'bullish' if score > 0 else 'bearish'}",
                          "summary": f"{sym}: {'; '.join(reasons)}. close {t['close']:.2f}.",
                          "direction": direction, "strength": min(0.7, abs(score)),
                          "source": "technical", "date": None})
    return notes
