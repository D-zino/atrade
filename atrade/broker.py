"""Broker layer: Alpaca Paper REST client and a deterministic offline MockBroker.

SAFETY: `alpaca_paper` is hard-defaulted to True; the client only ever points
at https://paper-api.alpaca.markets. Live-trading endpoints are not even coded.
"""
from __future__ import annotations

import json
import time as _time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from . import util

PAPER_BASE = "https://paper-api.alpaca.markets"
MARKET_BASE = "https://data.alpaca.markets"  # not needed for v2 bars w/ paper api


class BrokerError(Exception):
    pass


# ---------------------------------------------------------------------------
# Alpaca paper REST client (v2)
# ---------------------------------------------------------------------------
class AlpacaPaper:
    def __init__(self, api_key: str, secret_key: str, timeout: int = 20):
        self.api_key = api_key
        self.secret_key = secret_key
        self.timeout = timeout
        self.base = PAPER_BASE  # paper only

    # -- low level ---------------------------------------------------------
    def _request(self, method: str, path: str, params: dict | None = None,
                 body: dict | None = None) -> dict | list:
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, method=method)
        req.add_header("APCA-API-KEY-ID", self.api_key)
        req.add_header("APCA-API-SECRET-KEY", self.secret_key)
        req.add_header("Accept", "application/json")
        if body is not None:
            req.add_header("Content-Type", "application/json")
            data = json.dumps(body).encode()
        else:
            data = None
        try:
            with urllib.request.urlopen(req, data=data, timeout=self.timeout) as resp:
                raw = resp.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:400]
            except Exception:
                pass
            raise BrokerError(f"Alpaca HTTP {e.code} {path}: {detail}") from e
        except Exception as e:
            raise BrokerError(f"Alpaca network error {path}: {e}") from e

    # -- account / market --------------------------------------------------
    def account(self) -> dict:
        return self._request("GET", "/v2/account")

    def clock(self) -> dict:
        return self._request("GET", "/v2/clock")

    def positions(self) -> list:
        return self._request("GET", "/v2/positions")

    def assets(self, symbols: list[str]) -> dict[str, dict]:
        out = {}
        for sym in symbols:
            try:
                out[sym] = self._request("GET", f"/v2/assets/{sym}")
            except BrokerError:
                pass
        return out

    # -- bars (daily + intraday) --------------------------------------------
    def bars(self, symbols: list[str], timeframe: str = "1Day", limit: int = 60) -> dict[str, list]:
        """v2 bars endpoint. Returns {symbol: [bars]}."""
        if not symbols:
            return {}
        out = {}
        for i in range(0, len(symbols), 20):
            chunk = symbols[i:i + 20]
            try:
                data = self._request("GET", "/v2/bars", params={
                    "symbols": ",".join(chunk),
                    "timeframe": timeframe,
                    "limit": str(limit),
                    "adjustment": "raw",
                })
                for sym, bars in data.items():
                    out[sym] = bars
            except BrokerError:
                util.log(f"bars fetch failed for chunk {chunk}", "WARN")
        return out

    def quote(self, symbol: str) -> dict | None:
        try:
            return self._request("GET", f"/v2/stocks/{symbol}/quotes/latest")
        except BrokerError:
            return None

    # -- orders -------------------------------------------------------------
    def submit_order(self, symbol: str, qty: int, side: str, order_type: str = "market",
                     time_in_force: str = "day", limit_price: float | None = None) -> dict:
        side = {"long": "buy", "short": "sell"}.get(side, side)
        if side not in ("buy", "sell"):
            raise BrokerError(f"invalid side '{side}' for {symbol}")
        body = {"symbol": symbol, "qty": str(qty), "side": side,
                "type": order_type, "time_in_force": time_in_force}
        if limit_price is not None:
            body["type"] = "limit"
            body["limit_price"] = str(round(limit_price, 2))
        return self._request("POST", "/v2/orders", body=body)

    def orders(self, status: str = "all", limit: int = 100) -> list:
        return self._request("GET", "/v2/orders", params={"status": status, "limit": str(limit)})

    def cancel_all(self) -> None:
        try:
            self._request("DELETE", "/v2/orders")
        except BrokerError:
            pass


# ---------------------------------------------------------------------------
# MockBroker — fully offline simulation so the system runs without keys.
# Prices come from research snapshots (Yahoo/stooq). A small deterministic
# intraday drift is applied per (trading date, symbol) so demo day-trades
# produce realistic (non-zero) P&L between the open and close runs.
# ---------------------------------------------------------------------------
class MockBroker:
    def __init__(self, state_dir, initial_equity: float = 100000.0,
                 slippage_bps: float = 2.0, price_src: dict | None = None,
                 session: str = "open"):
        from pathlib import Path
        self.state_dir = Path(state_dir)
        self.initial_equity = float(initial_equity)
        self.slippage_bps = slippage_bps
        self.session = session  # "open" or "close" — drives pseudo-intraday drift
        # last known reference prices {symbol: price} seeded from research
        self.prices = dict(price_src or {})
        self._orders = []
        self._snap = util.read_json(state_dir / "mock_account.json") or None
        if self._snap is not None and "_drift_step" not in self._snap:
            self._snap["_drift_step"] = 0

    def _drift(self, symbol: str) -> float:
        """Deterministic pseudo-intraday move based on a per-run drift step.

        Each run (open/close, and each simulated day) advances the step, so
        consecutive runs see different prices — a simple random-walk market.
        Reproducible for a given step value.
        """
        a = self._acct()
        step = a.get("_drift_step", 0)
        key = f"{step}:{symbol}"
        h = 0
        for ch in key:
            h = (h * 31 + ord(ch)) & 0xFFFF
        # range roughly -1.2% .. +1.2% per step
        return ((h % 2400) - 1200) / 1000.0 * 0.012

    def get_price(self, symbol: str) -> float | None:
        base = self.prices.get(symbol)
        if base is None:
            return None
        return base * (1.0 + self._drift(symbol))

    def _acct(self) -> dict:
        if self._snap is None:
            self._snap = {"cash": self.initial_equity, "equity": self.initial_equity,
                          "positions": {}, "created": util.utc_iso()}
        return self._snap

    def _save(self) -> None:
        util.write_json(self.state_dir / "mock_account.json", self._snap)

    def seed_prices(self, prices: dict, session: str | None = None) -> None:
        if session:
            self.session = session
        self.prices.update({k: float(v) for k, v in prices.items() if v})
        a = self._acct()
        a["_drift_step"] = a.get("_drift_step", 0) + 1
        self._save()

    def account(self) -> dict:
        a = self._acct()
        eq = a["cash"] + sum(p["qty"] * (self.get_price(sym) or p.get("avg_entry", 0))
                             for sym, p in a["positions"].items())
        return {"equity": eq, "cash": a["cash"], "buying_power": eq,
                "currency": "USD", "status": "ACTIVE"}

    def positions(self) -> list:
        a = self._acct()
        out = []
        for sym, p in a["positions"].items():
            px = self.get_price(sym) or p["avg_entry"]
            out.append({"symbol": sym, "qty": p["qty"], "avg_entry_price": p["avg_entry"],
                        "current_price": px, "market_value": p["qty"] * px,
                        "unrealized_pl": (px - p["avg_entry"]) * p["qty"]})
        return out

    def bars(self, symbols, timeframe="1Day", limit=60) -> dict[str, list]:
        # In dry-run mode we synthesize a single bar from the drifted price.
        out = {}
        for sym in symbols:
            px = self.get_price(sym)
            if px:
                out[sym] = [{"t": util.utc_iso(), "o": px, "h": px * 1.002, "l": px * 0.998,
                             "c": px, "v": 1_000_000}]
        return out

    def submit_order(self, symbol, qty, side, order_type="market",
                     time_in_force="day", limit_price=None) -> dict:
        side = {"long": "buy", "short": "sell"}.get(side, side)  # normalize defensively
        if side not in ("buy", "sell"):
            raise BrokerError(f"invalid side '{side}' for {symbol}")
        px = self.get_price(symbol)
        if px is None:
            raise BrokerError(f"no reference price for {symbol}")
        slip = px * self.slippage_bps / 10_000.0
        fill = px - slip if side == "buy" else px + slip
        util.log(f"mock order: {side} {qty} {symbol} @ {fill:.4f} (session={self.session})", "DEBUG")
        a = self._acct()
        a["positions"].setdefault(symbol, {"qty": 0, "avg_entry": 0.0})
        p = a["positions"][symbol]
        if side == "buy":
            total_cost = fill * qty
            a["cash"] -= total_cost
            new_qty = p["qty"] + qty
            if new_qty == 0:
                del a["positions"][symbol]  # fully covered (long exit or short cover)
            else:
                p["avg_entry"] = (p["avg_entry"] * p["qty"] + total_cost) / new_qty
                p["qty"] = new_qty
        else:
            proceeds = fill * qty
            a["cash"] += proceeds
            p["qty"] -= qty
            if p["qty"] == 0:
                del a["positions"][symbol]
        self._save()
        order = {"id": f"mock-{_time.time_ns()}", "symbol": symbol, "qty": str(qty),
                 "side": side, "status": "filled", "filled_avg_price": str(round(fill, 4))}
        self._orders.append(order)
        return order

    def orders(self, status="all", limit=100) -> list:
        return self._orders[-limit:]

    def cancel_all(self) -> None:
        pass

    def clock(self) -> dict:
        return {"is_open": True, "timestamp": util.utc_iso()}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def make_broker(cfg: dict, state_dir, price_src: dict | None = None, force_mock: bool = False, session: str = "open"):
    """Return (broker, mode) where mode in {'alpaca_paper','mock'}."""
    keys = __import__("atrade.config", fromlist=["load_env_keys"]).load_env_keys()
    has_keys = bool(keys.get("ALPACA_API_KEY") and keys.get("ALPACA_SECRET_KEY"))
    if force_mock or cfg.get("broker") == "mock" or not has_keys:
        if not has_keys and not force_mock and cfg.get("broker") == "alpaca":
            util.log("No Alpaca keys found — falling back to MockBroker (dry-run). "
                     "Add keys to atrade/.env to go live on paper.", "WARN")
        return MockBroker(state_dir, initial_equity=cfg.get("initial_equity", 100000.0),
                          slippage_bps=cfg.get("slippage_bps", 2.0), price_src=price_src, session=session), "mock"
    return AlpacaPaper(keys["ALPACA_API_KEY"], keys["ALPACA_SECRET_KEY"]), "alpaca_paper"
