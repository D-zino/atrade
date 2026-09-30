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
from datetime import datetime, timezone

from . import util

PAPER_BASE = "https://paper-api.alpaca.markets"
MARKET_BASE = "https://data.alpaca.markets"  # not needed for v2 bars w/ paper api


class BrokerError(Exception):
    pass


def fetch_yahoo_spot(symbol: str, now: datetime | None = None,
                     max_age_minutes: float = 30.0) -> dict | None:
    """Fetch a recent Yahoo chart quote for an FX/commodity reference.

    The FX book calls this for ``XAUUSD=X`` first and ``GC=F`` second. A stale
    daily close is not treated as a tick: the result must have a market
    timestamp within ``max_age_minutes``. Returns ``{symbol, price, at}`` or
    ``None`` on fetch, parse, or freshness failure.
    """
    encoded = urllib.parse.quote(symbol, safe="=")
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}"
           "?range=1d&interval=1m&includePrePost=true")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; A-Trade-paper-book/1.0)"})
        with urllib.request.urlopen(req, timeout=12) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
        result = (((payload.get("chart") or {}).get("result") or [None])[0])
        if not result:
            return None
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        price = meta.get("regularMarketPrice")
        stamp = meta.get("regularMarketTime")
        if stamp is None:
            for ts, close in reversed(list(zip(timestamps, closes))):
                if close is not None:
                    stamp, price = ts, price or close
                    break
        if price is None or stamp is None:
            return None
        price = float(price)
        if price <= 0:
            return None
        quoted_at = datetime.fromtimestamp(float(stamp), tz=timezone.utc)
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            # This helper is normally passed an aware ET clock; treating a
            # naive input as ET keeps test/operator clocks consistent.
            from .market_fx import ET
            current = current.replace(tzinfo=ET).astimezone(timezone.utc)
        else:
            current = current.astimezone(timezone.utc)
        age_seconds = (current - quoted_at).total_seconds()
        if age_seconds < -300 or age_seconds > float(max_age_minutes) * 60:
            util.log(f"Yahoo {symbol} quote stale ({age_seconds / 60:.1f} min)", "WARN")
            return None
        return {"symbol": symbol, "price": price, "at": quoted_at.isoformat()}
    except Exception as exc:
        util.log(f"Yahoo {symbol} spot fetch failed: {exc}", "WARN")
        return None


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
                 session: str = "open", *, spread: float = 0.0,
                 fractional_qty: bool = False,
                 account_filename: str = "mock_account.json",
                 drift_scale: float = 1.0):
        from pathlib import Path
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.initial_equity = float(initial_equity)
        self.slippage_bps = float(slippage_bps)
        self.session = session  # "open" or "close" — drives pseudo-intraday drift
        self.spread = max(0.0, float(spread))
        self.fractional_qty = bool(fractional_qty)
        self.account_filename = account_filename
        self.account_path = self.state_dir / self.account_filename
        self.drift_scale = float(drift_scale)
        # last known reference prices {symbol: price} seeded from research
        self.prices = dict(price_src or {})
        self._orders = []
        self._snap = util.read_json(self.account_path) or None
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
        # range roughly -1.2% .. +1.2% per step before instrument scaling.
        # XAUUSD config uses a smaller drift_scale than the equity mock.
        return ((h % 2400) - 1200) / 1000.0 * 0.012 * self.drift_scale

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
        util.write_json(self.account_path, self._snap)

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
        if self.fractional_qty:
            qty = round(float(qty), 3)
            if qty <= 0:
                raise BrokerError(f"quantity must be positive for {symbol}")
        slip = px * self.slippage_bps / 10_000.0
        if self.spread:
            # Adverse paper fills at ask for buys and bid for sells. The
            # spread is the full quoted spread (XAUUSD default: $0.30).
            half_spread = self.spread / 2.0
            fill = px + half_spread + slip if side == "buy" else px - half_spread - slip
        else:
            # Keep the historical equity mock's fill convention unchanged.
            fill = px - slip if side == "buy" else px + slip
        util.log(f"mock order: {side} {qty} {symbol} @ {fill:.4f} (session={self.session})", "DEBUG")
        a = self._acct()
        a["positions"].setdefault(symbol, {"qty": 0, "avg_entry": 0.0})
        p = a["positions"][symbol]
        if self.fractional_qty:
            self._apply_fractional_order(a, symbol, p, qty, side, fill)
        elif side == "buy":
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

    @staticmethod
    def _apply_fractional_order(account: dict, symbol: str, position: dict,
                                 qty: float, side: str, fill: float) -> None:
        """Apply a fractional order with signed positions (used by FX only)."""
        old_qty = float(position.get("qty") or 0.0)
        old_entry = float(position.get("avg_entry") or 0.0)
        eps = 0.0005
        if side == "buy":
            account["cash"] -= fill * qty
            new_qty = old_qty + qty
            if abs(new_qty) < eps:
                account["positions"].pop(symbol, None)
            elif old_qty < -eps and new_qty > eps:
                # A buy first covers a short. Any excess starts a new long.
                position.update(qty=round(new_qty, 3), avg_entry=fill)
            elif old_qty < -eps:
                position["qty"] = round(new_qty, 3)
            else:
                position["avg_entry"] = ((old_entry * old_qty + fill * qty) / new_qty
                                          if new_qty > 0 else fill)
                position["qty"] = round(new_qty, 3)
        else:
            account["cash"] += fill * qty
            new_qty = old_qty - qty
            if abs(new_qty) < eps:
                account["positions"].pop(symbol, None)
            elif old_qty > eps and new_qty < -eps:
                # A sell first closes a long. Any excess starts a short.
                position.update(qty=round(new_qty, 3), avg_entry=fill)
            elif old_qty > eps:
                position["qty"] = round(new_qty, 3)
            else:
                new_short_size = abs(new_qty)
                old_short_size = abs(old_qty)
                position["avg_entry"] = ((old_entry * old_short_size + fill * qty) /
                                          new_short_size if new_short_size else fill)
                position["qty"] = round(new_qty, 3)

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
