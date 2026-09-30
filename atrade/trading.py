"""Trading execution: risk sizing, order placement, day-trade close-out.

Rules:
  - day-trade only: every position opened at the open-run is closed at the close-run
  - max N concurrent positions (default 5), max total exposure (default 60% of equity)
  - position size interpolated from hypothesis confidence (see cfg.conf_to_size)
  - intraday stop: ~2% adverse move (ATR-ish proxy) triggers a defensive close
"""
from __future__ import annotations

from . import util


def size_for(cfg: dict, confidence: float, equity: float, symbol_price: float | None) -> float:
    """Dollar size for a position given confidence."""
    conf_to_size = {float(k): float(v) for k, v in cfg.get("conf_to_size",
                    {0.60: 0.09, 0.70: 0.12, 0.80: 0.15}).items()}
    keys = sorted(conf_to_size)
    c = max(keys[0], min(float(confidence), 1.0))
    # linear interpolation between config points
    pct = conf_to_size[keys[0]] if len(keys) == 1 else None
    if pct is None:
        for a, b in zip(keys, keys[1:]):
            if c <= b:
                pct = conf_to_size[a] + (conf_to_size[b] - conf_to_size[a]) * (c - a) / (b - a)
                break
        else:
            pct = conf_to_size[keys[-1]]
    dollars = equity * pct
    if symbol_price and symbol_price > 0:
        qty = max(1, int(dollars / symbol_price))
        dollars = qty * symbol_price
    return max(cfg.get("min_trade_value", 200.0), min(cfg.get("max_trade_value", 80000.0), dollars))


def warmup_factor(cfg: dict, n_trades: int) -> float:
    """Warm-up sizing multiplier: 40%..100% as the first N trades accumulate."""
    until = int(cfg.get("warmup_until_trades", 12))
    lo = float(cfg.get("warmup_min_factor", 0.40))
    return round(lo + (1.0 - lo) * min(1.0, n_trades / until), 3)


def open_positions(broker, cfg: dict, hyps: list[dict], equity: float,
                   existing: list[dict], price_map: dict,
                   warmup_factor_override: float = 1.0,
                   daily_pnl: float = 0.0) -> tuple[list[dict], list[str]]:
    """Open new positions for ranked hypotheses. Returns (opened, skipped_reasons).

    daily_pnl: today's P&L (realized + unrealized). If it breaches the daily
    loss limit, no new trades are opened for the day (existing ones still close
    at the close run).
    """
    opened, skipped = [], []
    # daily loss limit guard
    dll = cfg.get("daily_loss_limit_pct")
    if dll:
        limit = float(dll)
        if daily_pnl < -limit * equity:
            skipped.append(f"daily loss limit hit (today {daily_pnl:.0f} < -{limit*100:.0f}% of equity)")
            return opened, skipped
    max_pos = cfg.get("max_positions", 2)
    max_pct_total = cfg.get("max_portfolio_pct", 0.24)
    open_count = len(existing)
    deployed = sum(float(p.get("market_value", 0)) for p in existing)
    deployable = equity * max_pct_total - deployed
    if deployable < cfg.get("min_trade_value", 200.0):
        skipped.append("portfolio exposure cap reached")
        return opened, skipped
    # correlation clusters: don't stack too many positions in one cluster
    clusters = cfg.get("correlation_clusters") or {}
    max_per_cluster = int(cfg.get("max_per_cluster", 2))
    cluster_counts = {}
    for e in existing:
        esym = e.get("symbol")
        for cl, members in clusters.items():
            if esym in members:
                cluster_counts[cl] = cluster_counts.get(cl, 0) + 1
                break
    for h in hyps:
        if open_count >= max_pos:
            skipped.append(f"max positions ({max_pos}) reached")
            break
        sym = h["symbol"]
        sym_cluster = next((cl for cl, members in clusters.items() if sym in members), None)
        if sym_cluster and cluster_counts.get(sym_cluster, 0) >= max_per_cluster:
            skipped.append(f"{sym}: cluster '{sym_cluster}' already at {max_per_cluster} positions")
            continue
        px = price_map.get(sym)
        if not px or px <= 0:
            skipped.append(f"{sym}: no reference price")
            continue
        dollars = size_for(cfg, h["confidence"], equity, px) * warmup_factor_override
        if dollars > deployable:
            skipped.append(f"{sym}: exposure cap (need {dollars:.0f}, have {deployable:.0f})")
            continue
        qty = max(1, int(dollars / px))
        side = h["side"]
        order_side = "buy" if side == "long" else "sell"  # broker API wants buy/sell
        try:
            order = broker.submit_order(sym, qty, order_side)
        except Exception as e:
            util.log(f"order failed {sym}: {e}", "WARN")
            skipped.append(f"{sym}: order error {e}")
            continue
        trade = {
            "trade_id": f"{sym}-{util.utc_iso()}",
            "symbol": sym, "side": side, "qty": qty,
            "warmup_factor": warmup_factor_override,
            "opened_at": util.utc_iso(), "entry_price": float(order.get("filled_avg_price") or px),
            "order_id": order.get("id"),
            "hypothesis": {k: h.get(k) for k in ("thesis", "falsifiers", "confidence", "dominant_category", "evidence")},
            "hypothesis_id": None,
            "status": "open",
            "pnl": None, "pnl_pct": None, "exit_price": None, "closed_at": None,
            "stop_hit": False, "notes": [],
        }
        opened.append(trade)
        open_count += 1
        deployed += dollars
        deployable = equity * max_pct_total - deployed
        if sym_cluster:
            cluster_counts[sym_cluster] = cluster_counts.get(sym_cluster, 0) + 1
        util.log(f"OPEN {side.upper()} {qty} {sym} @ {trade['entry_price']:.2f} "
                 f"(conf {h['confidence']:.2f}, ${dollars:.0f})"
                 + (f" [cluster {sym_cluster}]" if sym_cluster else ""))
        if open_count >= max_pos:
            break
    return opened, skipped


def _flatten(existing: list[dict]) -> dict[str, dict]:
    return {p.get("symbol"): p for p in existing}


def close_day_trades(broker, cfg: dict, ledger: list[dict], positions: list[dict],
                     price_map: dict) -> list[dict]:
    """Close every open position; update ledger trades with realized P&L.

    Returns the list of closed trade dicts (status -> 'closed'/'stop').
    """
    closed = []
    by_symbol = _flatten(positions)
    open_trades = [t for t in ledger if t.get("status") == "open"]
    for t in open_trades:
        sym = t["symbol"]
        pos = by_symbol.get(sym)
        px = None
        if pos:
            px = float(pos.get("current_price") or 0) or price_map.get(sym)
        else:
            px = price_map.get(sym)
        if not px:
            px = t.get("entry_price")
        qty = int(t.get("qty") or (pos.get("qty") if pos else 0) or 0)
        if qty <= 0:
            t["status"] = "cancelled"
            t["notes"].append("no position to close; marked cancelled")
            closed.append(t)
            continue
        try:
            # longs are closed with sell; shorts are closed with buy-to-cover
            close_side = "sell" if t["side"] == "long" else "buy"
            order = broker.submit_order(sym, qty, close_side)
            fill = float(order.get("filled_avg_price") or px)
        except Exception as e:
            util.log(f"close failed {sym}: {e}", "WARN")
            t["notes"].append(f"close order error: {e}")
            closed.append(t)
            continue
        entry = float(t.get("entry_price") or 0)
        if entry > 0:
            sign = 1 if t["side"] == "long" else -1
            t["pnl"] = round((fill - entry) * qty * sign, 2)
            t["pnl_pct"] = round(sign * (fill / entry - 1.0), 4)
        else:
            t["pnl"] = 0.0
            t["pnl_pct"] = 0.0
        t["exit_price"] = round(fill, 4)
        t["closed_at"] = util.utc_iso()
        t["status"] = "closed"
        t["notes"].append("closed at scheduled close run")
        util.log(f"CLOSE {t['side'].upper()} {qty} {sym} @ {fill:.2f} P&L {t['pnl']:+.2f} "
                 f"({t['pnl_pct']*100:+.2f}%)")
        closed.append(t)
    return closed


def intraday_stop_check(broker, cfg: dict, ledger: list[dict], positions: list[dict],
                        price_map: dict) -> list[dict]:
    """Defensive intraday stop: close trades that moved > ~1.4% against entry."""
    closed = []
    by_symbol = _flatten(positions)
    for t in [x for x in ledger if x.get("status") == "open"]:
        sym = t["symbol"]
        pos = by_symbol.get(sym)
        px = None
        if pos:
            px = float(pos.get("current_price") or 0) or price_map.get(sym)
        else:
            px = price_map.get(sym)
        if not px:
            continue
        entry = float(t.get("entry_price") or 0)
        if entry <= 0:
            continue
        move = (px / entry - 1.0) * (1 if t["side"] == "long" else -1)
        stop = float(cfg.get("intraday_stop_pct", 0.014))
        if move <= -stop:
            qty = int(t.get("qty") or 0)
            close_side = "sell" if t["side"] == "long" else "buy"
            try:
                broker.submit_order(sym, qty, close_side)
            except Exception:
                continue
            t["pnl"] = round((px - entry) * qty * (1 if t["side"] == "long" else -1), 2)
            t["pnl_pct"] = round(-move, 4)
            t["exit_price"] = round(px, 4)
            t["closed_at"] = util.utc_iso()
            t["status"] = "closed"
            t["stop_hit"] = True
            t["notes"].append("intraday stop hit (defensive close)")
            util.log(f"STOP {sym} @ {px:.2f} P&L {t['pnl']:+.2f}")
            closed.append(t)
    return closed


# ---------------------------------------------------------------------------
# XAUUSD swing-with-stops helpers (additive; equity order paths above unchanged)
# ---------------------------------------------------------------------------

def _bar_value(bar: dict, *keys: str) -> float | None:
    for key in keys:
        try:
            value = bar.get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def atr_24h(bars: list[dict], period: int = 14) -> float | None:
    """Simple mean true range over completed 24h bars (USD per troy ounce)."""
    if not bars or period <= 0:
        return None
    ranges = []
    previous_close = None
    for bar in bars:
        high = _bar_value(bar, "h", "high")
        low = _bar_value(bar, "l", "low")
        close = _bar_value(bar, "c", "close")
        if high is None or low is None or high < low:
            continue
        tr = high - low
        if previous_close is not None:
            tr = max(tr, abs(high - previous_close), abs(low - previous_close))
        ranges.append(tr)
        if close is not None:
            previous_close = close
    if not ranges:
        return None
    recent = ranges[-period:]
    return sum(recent) / len(recent)


def latest_confirmed_swing(bars: list[dict], side: str,
                           pivot_bars: int = 3) -> float | None:
    """Return the latest confirmed 3-bar swing low/high.

    A pivot is confirmed only after a bar has closed on each side of it. The
    default is deliberately the 3-bar pivot selected for the XAUUSD book; an
    unconfirmed latest candle is never used as structure.
    """
    if pivot_bars != 3:
        raise ValueError("XAUUSD initial structure uses confirmed 3-bar pivots")
    if len(bars or []) < 3 or side not in {"long", "short"}:
        return None
    field = ("l", "low") if side == "long" else ("h", "high")
    for index in range(len(bars) - 2, 0, -1):
        current = _bar_value(bars[index], *field)
        before = _bar_value(bars[index - 1], *field)
        after = _bar_value(bars[index + 1], *field)
        if current is None or before is None or after is None:
            continue
        if side == "long" and current < before and current < after:
            return current
        if side == "short" and current > before and current > after:
            return current
    return None


def swing_initial_stop(entry_price: float, side: str, bars: list[dict],
                       atr: float | None, atr_mult: float = 3.0,
                       pivot_bars: int = 3) -> dict | None:
    """Build a stop from a confirmed structure pivot and 3x 24h ATR.

    For a long, select the higher valid level (closer to entry) from the swing
    low and ``entry - atr_mult*ATR``. For a short, select the lower valid level
    from the swing high and ``entry + atr_mult*ATR``. This is the stop rule
    confirmed for the v1 book; narrative falsifiers remain metadata.
    """
    try:
        entry = float(entry_price)
        atr_value = float(atr) if atr is not None else 0.0
        mult = float(atr_mult)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or atr_value <= 0 or mult <= 0 or side not in {"long", "short"}:
        return None
    structure = latest_confirmed_swing(bars, side, pivot_bars=pivot_bars)
    atr_level = entry - mult * atr_value if side == "long" else entry + mult * atr_value
    candidates = [atr_level]
    if structure is not None:
        if (side == "long" and 0 < structure < entry) or (side == "short" and structure > entry):
            candidates.append(structure)
    stop = max(candidates) if side == "long" else min(candidates)
    if (side == "long" and not 0 < stop < entry) or (side == "short" and stop <= entry):
        return None
    stop = round(stop, 2)
    if (side == "long" and not 0 < stop < entry) or (side == "short" and stop <= entry):
        return None
    if structure not in candidates:
        stop_basis = "3x_atr_fallback"
    elif abs(stop - structure) < 0.0001:
        stop_basis = "confirmed_3bar_pivot"
    else:
        stop_basis = "3x_atr_tighter_than_pivot"
    return {
        "stop_price": stop,
        "structure_price": round(structure, 2) if structure is not None else None,
        "atr_price": round(atr_level, 2),
        "stop_basis": stop_basis,
        "atr": round(atr_value, 4),
        "atr_mult": mult,
    }


def swing_size(equity: float, risk_pct: float, entry_price: float,
               stop_price: float, precision: int = 3) -> float:
    """Risk-size a paper position in fractional troy ounces.

    ``oz = equity * risk_pct / abs(entry - stop)``. Quantity is rounded down
    to avoid exceeding the requested dollar risk.
    """
    try:
        equity_value = float(equity)
        risk = float(risk_pct)
        distance = abs(float(entry_price) - float(stop_price))
    except (TypeError, ValueError):
        return 0.0
    if equity_value <= 0 or risk <= 0 or distance <= 0:
        return 0.0
    scale = 10 ** max(0, int(precision))
    return max(0.0, int((equity_value * risk / distance) * scale) / scale)


def _broker_position_qty(broker, symbol: str) -> float | None:
    if not hasattr(broker, "positions"):
        return None
    try:
        for position in broker.positions() or []:
            if position.get("symbol") == symbol:
                return float(position.get("qty") or 0.0)
        return 0.0
    except Exception as exc:
        util.log(f"FX broker position lookup failed for {symbol}: {exc}", "WARN")
        return None


def _close_swing_position(broker, trade: dict, market_price: float,
                          exit_reason: str, now=None) -> dict | None:
    symbol = trade.get("symbol") or "XAUUSD"
    side = trade.get("side")
    qty = float(trade.get("qty_oz") or trade.get("qty") or 0.0)
    entry = float(trade.get("entry_price") or 0.0)
    if side not in {"long", "short"} or qty <= 0 or entry <= 0:
        return None
    position_qty = _broker_position_qty(broker, symbol)
    expected_sign = 1.0 if side == "long" else -1.0
    if position_qty is not None and (
        abs(abs(position_qty) - qty) > 0.0005 or position_qty * expected_sign <= 0
    ):
        util.log(f"FX close blocked: broker position mismatch for {symbol} "
                 f"(ledger {expected_sign * qty:.3f}, broker {position_qty:.3f})", "ERROR")
        return None
    close_side = "sell" if side == "long" else "buy"
    try:
        order = broker.submit_order(symbol, qty, close_side)
        fill = float(order.get("filled_avg_price") or market_price)
    except Exception as exc:
        util.log(f"FX close failed for {symbol}: {exc}", "ERROR")
        return None
    sign = 1.0 if side == "long" else -1.0
    pnl = (fill - entry) * qty * sign
    pnl_pct = sign * (fill / entry - 1.0)
    if now is None:
        closed_at = util.utc_iso()
    else:
        closed_at = now.isoformat()
    trade.update({
        "status": exit_reason,
        "exit_reason": exit_reason,
        "exit_price": round(fill, 4),
        "closed_at": closed_at,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 6),
        "stop_hit": exit_reason == "stopped",
    })
    notes = trade.setdefault("notes", [])
    if isinstance(notes, list):
        notes.append(f"XAUUSD swing exit: {exit_reason}")
    else:
        trade["notes"] = [str(notes), f"XAUUSD swing exit: {exit_reason}"]
    util.log(f"GOLD {exit_reason.upper()} {side.upper()} {qty:.3f} oz @ {fill:.2f} "
             f"P&L {pnl:+.2f}")
    return trade


def check_stops(broker, cfg: dict, ledger: list[dict], price_map: dict,
                now=None) -> list[dict]:
    """Enforce XAUUSD stop/trail/target levels on each dispatcher tick."""
    closed = []
    for trade in [t for t in ledger if t.get("status") == "open" and
                  t.get("symbol") == "XAUUSD"]:
        try:
            price = float(price_map.get("XAUUSD"))
        except (TypeError, ValueError):
            continue
        side = trade.get("side")
        stop_levels = []
        for key in ("stop_price", "trail_price"):
            try:
                level = float(trade.get(key))
                if level > 0:
                    stop_levels.append((key, level))
            except (TypeError, ValueError):
                pass
        breached = None
        if stop_levels:
            effective = (max(level for _, level in stop_levels) if side == "long"
                         else min(level for _, level in stop_levels))
            if (side == "long" and price <= effective) or (side == "short" and price >= effective):
                kind = "trail" if any(name == "trail_price" and abs(level - effective) < 0.0001
                                        for name, level in stop_levels) else "initial"
                breached = ("stopped", kind)
        if breached is None:
            try:
                target = float(trade.get("take_profit"))
            except (TypeError, ValueError):
                target = 0.0
            if target > 0 and ((side == "long" and price >= target) or
                               (side == "short" and price <= target)):
                breached = ("target", None)
        if breached:
            reason, stop_kind = breached
            if stop_kind:
                trade["stop_kind"] = stop_kind
            result = _close_swing_position(broker, trade, price, reason, now)
            if result is not None:
                closed.append(result)
    return closed


def update_entry_extrema(ledger: list[dict], price_map: dict) -> None:
    """Maintain chandelier high/low watermarks without moving stops on ticks."""
    try:
        price = float(price_map.get("XAUUSD"))
    except (TypeError, ValueError):
        return
    for trade in ledger:
        if trade.get("status") != "open" or trade.get("symbol") != "XAUUSD":
            continue
        entry = float(trade.get("entry_price") or price)
        current = trade.get("entry_extremum")
        try:
            current = float(current) if current is not None else entry
        except (TypeError, ValueError):
            current = entry
        trade["entry_extremum"] = max(current, price) if trade.get("side") == "long" else min(current, price)


def trail_stops(ledger: list[dict], price_map: dict, atr: float | None,
                cfg: dict) -> list[dict]:
    """Ratchet chandelier trails during scheduled risk-checks only."""
    try:
        price = float(price_map.get("XAUUSD"))
        atr_value = float(atr) if atr is not None else 0.0
    except (TypeError, ValueError):
        return []
    if price <= 0 or atr_value <= 0:
        return []
    updates = []
    for trade in ledger:
        if trade.get("status") != "open" or trade.get("symbol") != "XAUUSD":
            continue
        side = trade.get("side")
        entry = float(trade.get("entry_price") or 0.0)
        if entry <= 0 or side not in {"long", "short"}:
            continue
        extremum = float(trade.get("entry_extremum") or entry)
        extremum = max(extremum, price) if side == "long" else min(extremum, price)
        trade["entry_extremum"] = extremum
        multiplier = float(trade.get("trail_atr_mult") or cfg.get("trail_atr_mult", 3.0))
        candidate = extremum - multiplier * atr_value if side == "long" else extremum + multiplier * atr_value
        old = trade.get("trail_price")
        old_value = float(old) if old is not None else None
        new_value = candidate if old_value is None else (
            max(old_value, candidate) if side == "long" else min(old_value, candidate))
        if cfg.get("breakeven_after_1r"):
            initial_risk = float(trade.get("initial_risk_per_oz") or 0.0)
            signed_move = (price - entry) * (1 if side == "long" else -1)
            if initial_risk > 0 and signed_move >= initial_risk:
                new_value = max(new_value, entry) if side == "long" else min(new_value, entry)
        new_value = round(new_value, 2)
        if old_value is None or abs(new_value - old_value) >= 0.01:
            trade["trail_price"] = new_value
            updates.append({"symbol": "XAUUSD", "side": side,
                            "old": round(old_value, 2) if old_value is not None else None,
                            "new": new_value, "atr": round(atr_value, 4),
                            "entry_extremum": round(extremum, 2)})
    return updates


def close_swing_positions(broker, ledger: list[dict], price_map: dict,
                          exit_reason: str, now=None) -> list[dict]:
    """Close all XAUUSD swing positions for an explicit policy exit."""
    if exit_reason not in {"weekend_flat", "thesis_broken", "closed"}:
        raise ValueError(f"unsupported explicit XAUUSD exit reason: {exit_reason}")
    try:
        price = float(price_map.get("XAUUSD"))
    except (TypeError, ValueError):
        return []
    closed = []
    for trade in [t for t in ledger if t.get("status") == "open" and
                  t.get("symbol") == "XAUUSD"]:
        result = _close_swing_position(broker, trade, price, exit_reason, now)
        if result is not None:
            closed.append(result)
    return closed
