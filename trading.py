"""Trading execution: risk sizing, order placement, day-trade close-out.

Rules:
  - day-trade only: every position opened at the open-run is closed at the close-run
  - max N concurrent positions (default 2), max total exposure (default 24% of equity)
  - position size interpolated from hypothesis confidence (see cfg.conf_to_size)
  - intraday stop: ~1.4% adverse move (ATR-ish proxy) triggers a defensive close
"""
from __future__ import annotations

from . import util


def size_for(cfg: dict, confidence: float, equity: float, symbol_price: float | None) -> float:
    """Dollar size for a position given confidence."""
    conf_to_size = {float(k): float(v) for k, v in cfg.get("conf_to_size",
                    {0.60: 0.06, 0.70: 0.09, 0.80: 0.12}).items()}
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
    return max(cfg.get("min_trade_value", 200.0), min(cfg.get("max_trade_value", 20000.0), dollars))


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
    for h in hyps:
        if open_count >= max_pos:
            skipped.append(f"max positions ({max_pos}) reached")
            break
        sym = h["symbol"]
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
        util.log(f"OPEN {side.upper()} {qty} {sym} @ {trade['entry_price']:.2f} "
                 f"(conf {h['confidence']:.2f}, ${dollars:.0f})")
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
