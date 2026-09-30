# XAUUSD (spot gold) — design for a swing-with-stops book

**Status: design only. No trading code exists for this yet.** This document is
the contract for a follow-up PR/session. PR "dispatch windows + catch-up"
stays equities-only.

Decisions locked in (from the planning discussion):

| Decision | Choice |
|---|---|
| Instrument | XAUUSD (spot gold), single-instrument book to start |
| Holding style | **Swing with stops** — positions held across days; the daily "close run" becomes a **risk-check**; exits come from stop / target / thesis-broken, not from an end-of-day flatten |
| Venue | **Paper first** — extend `MockBroker`; no broker signup required. A live FX venue (OANDA-style REST) is phase 2 |

---

## 1. Why the equity schedule can't be reused as-is

The current system assumes the NYSE cash session: one trading day, 09:30–16:00
ET, flatten everything at the close run, once-per-day markers keyed to the
calendar date. Spot gold trades **24/5** (Sun ~17:00 ET → Fri ~17:00 ET, with a
short daily maintenance break ~17:00–18:00 ET; exact break is broker-specific).
Concretely:

- **There is no "close".** A 15:50 ET flatten is mid-afternoon in the most
  liquid part of the gold day (London–NY overlap). Swing positions must not be
  force-flattened daily — that is the whole point of the swing book.
- **The day boundary is the rollover (17:00 ET), not midnight.** Markers keyed
  to the calendar date break around it (a position opened Sunday 18:00 ET is
  "Monday" in ET terms; the FX day that started Mon 17:00 ET ends Tue 17:00 ET).
- **A missed window is more dangerous.** Equities get force-flattened by the
  market close; gold runs 24h with nobody at the desk. Stop protection cannot
  live inside a 20-minute (or even 3-hour) window.
- **Weekend gap risk** exists (Fri 17:00 → Sun 17:00) and needs an explicit
  policy.

Everything *around* the schedule ports cleanly: the dispatch skeleton
(windows + once-per-day markers + catch-up), the engine loop (research →
hypotheses → risk sizing → manage → evaluate → learn), the evaluator/playbook,
and the entire Telegram layer.

## 2. Market model (`atrade/market_fx.py` — new file, `market.py` untouched)

```
FX trading day: 17:00 ET → next day 17:00 ET   (the "rollover day")
Week:           Sun 17:00 ET reopen → Fri 17:00 ET close
Daily break:    ~17:00–18:00 ET (venue maintenance; paper book treats it as closed)
```

- `is_fx_trading_day(d)`: day rolls at 17:00 ET, so `fx_day(now_et)` returns
  the rollover-date label used by markers and the ledger (e.g. Sun 18:00 ET →
  Monday's FX day).
- FX holiday calendar is thin compared to NYSE: Jan 1, Dec 25, and a few
  venue-specific closures. Fri 17:00 / Sun 17:00 are the structural boundaries
  that matter.
- `fx_session(now_et)` classifies a moment: `asia` (18:00–03:00),
  `tokyo` (19:00–04:00), `london` (03:00–11:30), `ny` (08:00–17:00),
  `overlap` (08:00–11:30 — highest liquidity/volatility for gold),
  `break` (17:00–18:00), `weekend`.
- High-impact event awareness (already half-built): NFP, CPI, PPI, FOMC, ECB,
  BoE — `upcoming_events()` exists in the engine; the FX preview should surface
  them with a "no new entries ±15 min around red events" guard.

## 3. Concept mapping: equity book → XAUUSD book

| Equity (today) | XAUUSD (this design) |
|---|---|
| `open_run` at 09:25 | `swing_open_run` — research, thesis, entries + initial stops |
| `checkin_run` at 10:30 | `risk_check_run` — position table, trail updates (per session block) |
| `close_run` at 15:50 (flatten all) | **no flatten** — exits are stop / target / thesis-broken / weekend policy |
| intraday 1.4% defensive stop | per-trade `stop_price` / `trail_price`, enforced on **every dispatch tick** |
| `preview_run` at 20:00 | `session_preview_run` — next session plan + FX calendar |
| `week_ahead_run` Sunday | same shape, FX macro backdrop (DXY, real yields, central-bank calendar) |
| once-per-day markers (ET date) | once-per-**FX-day** markers (rollover date) |
| catch-up close | **catch-up risk-check** + tick-level stop enforcement (see §5) |
| `close_day_trades` → realized P&L, `hypothesis_correct` | grading happens **per exit**, not per day; daily score is mark-to-market |
| qty = shares, `$ risk` on equity price | qty = **troy ounces**, `oz = equity × risk% / (entry − stop)` |
| NYSE calendar `market.py` | FX calendar `market_fx.py` |

## 4. Dispatch schedule (`deploy/dispatch_fx.py` or `dispatch.py --book xauusd`)

Windows (ET), deliberately wide (GH cron lateness lesson learned):

| Run | Window (ET) | Marker (per FX day) | Notes |
|---|---|---|---|
| stop enforcement | **every tick** | none | cheap idempotent check, see §5 |
| swing open | 08:05–11:00 | `open` | best liquidity (London–NY overlap) |
| risk-check (London) | 03:05–05:30 | `checkin_am` | trail management, Europe session read |
| risk-check (NY + pre-break) | 14:00–16:45 | `checkin_pm` | **mandatory pre-rollover pass** — tighten stops / flatten risk before the thin break |
| session preview | 18:05–20:30 | `preview` | after the 17:00–18:00 break reopens |
| week-ahead | Sun 17:30–19:30 | `week_ahead` | Sunday only, after the weekly reopen |

Catch-up (same philosophy as the equity fix):

1. **Missed risk-check window** and positions are open → run it late on the
   next tick (up to the end of that session block; `close_catchup`-style
   once-per-block marker so it can't loop).
2. **Missed swing-open window** and no entries yet this FX day → run it on the
   next tick still inside any later block (e.g. a 13:40 tick catches up with
   entries + initial stops).
3. **Stale theses**: if a position has been open longer than its declared
   `thesis_horizon` and no exit ran, the risk-check escalates it (message +
   forced re-grade) instead of leaving it to drift — the swing-book equivalent
   of the NVDA 2 Sep → 28 Sep incident.

State/ledger isolation: `state/state_fx.json`, `state/last_dispatch_fx.json`,
`state/mock_account_fx.json` — the equity book and its workflow stay untouched.
A new workflow `atrade_fx.yml` with a near-24h cron (e.g. `*/15`); `atrade.yml`
is not modified.

## 5. Swing position model & stop engine

Trade dict gains (all persisted in the FX ledger):

```json
{
  "symbol": "XAUUSD",
  "side": "long",
  "qty_oz": 22.4,
  "entry_price": 4180.5,
  "stop_price": 4128.0,
  "trail_price": null,
  "trail_atr_mult": 3.0,
  "take_profit": 4295.0,
  "thesis_horizon": "2-5 sessions",
  "opened_at": "…", "fx_day": "2026-10-06",
  "status": "open"
}
```

Lifecycle: `open → stopped | target | thesis_broken | weekend_flat | closed`.
Every exit records `exit_reason`, realized P&L, and grades the hypothesis
(`hypothesis_correct`, `lesson`) exactly like the equity close does today.

**Stop enforcement runs on every dispatch tick** (not inside a window): price
from the broker/mock feed is compared to `stop_price` / `trail_price`; on a
breach the position is flattened at market and a 🛑 Telegram alert fires.
This is what makes "missed window" survivable in a 24h market — the safety net
is no longer schedule-bound. (MockBroker has no native stop orders; the check
lives in `trading.check_stops()`, mirroring the existing
`intraday_stop_check` but per-trade instead of a flat 1.4%.)

Trailing rules (applied in risk-check runs only, to avoid churn):

- **Initial stop**: structure stop (recent session swing low/high) or
  `k × ATR(24h)`, whichever is tighter to the thesis falsifier. The falsifier
  field already exists on every hypothesis — the stop is its price translation.
- **Trail**: chandelier `entry_extremum ∓ 3 × ATR(24h)`; optional breakeven
  move after +1R.
- **Take-profit**: optional; partial at 2R is a config knob.

**Sizing** (risk-based, replaces share-qty sizing):

```
oz = equity × risk_pct / |entry − stop|          (risk_pct default 1.0–1.5%)
```

Caps: max 2 concurrent XAUUSD positions, `max_per_cluster` for
`precious_metals` already exists (GLD/SLV + XAUUSD share the cluster).

**Weekend policy** (config, default `flatten_before_weekend: true` for v1):
Friday `checkin_pm` flattens all risk before the 17:00 close; the alternative
(`hold_with_tightened_stops`) keeps positions with stops moved to breakeven —
off by default because of gap risk.

## 6. Evaluation & learning under swing holding

- Daily composite score keeps working: it becomes mark-to-market (unrealized
  P&L already feeds `_daily_pnl` and the drawdown guard).
- Realized grading (`hypothesis_correct`, lessons, signal-tracker updates)
  moves to **exit events** — `learning.run_learning` is called per exit batch
  instead of per close. Score history entries can note `mark: true` vs
  `realized: true`.
- The drawdown/auto-pause machinery (`peak_equity`, `max_drawdown_pct`)
  transfers unchanged — it matters more when positions run overnight.
- `PLAYBOOK.md` gains an XAUUSD section (the playbook generator already
  takes `all_closed`).

## 7. Paper venue (phase 1)

Extend `MockBroker` rather than adding a new class:

- **Price feed**: research already fetches `GC=F` on every pass
  (`price_universe` in `research.py`) — seed mock fills from it; optionally add
  `XAUUSD=X` (Yahoo spot) as the closer-to-broker reference. Drift walk
  reuses `_drift`, scaled to gold vol.
- **Spread**: fixed 0.30 (~typical XAUUSD retail spread), fills at
  bid/ask ± `slippage_bps` as today.
- **Qty in troy ounces**, fractional allowed in paper mode (1 "lot" = 100 oz).
- **Stop fills**: engine-enforced (§5); the mock just provides prices each tick.
- **Swap/carry**: ignored in phase 1 (documented); phase 2 models it when the
  live adapter lands.

## 8. Telegram message set

| When | Message | Contents |
|---|---|---|
| swing open | 🟢 **GOLD — SWING OPEN** | entries (side, oz, price), initial stop, risk $, thesis + falsifier, ATR context |
| risk-check (2×/FX day) | 🟡 **GOLD — RISK CHECK** | position table (entry / stop / trail / uP&L), trail adjustments made, events in play |
| any tick, on exit | 🛑 **GOLD — STOPPED / TARGET / THESIS BROKEN** | exit price, realized P&L, ✅/❌ grade of the thesis, lesson |
| session preview | 🌙 **GOLD — SESSION PREVIEW** | next session(s), FX calendar (NFP/CPI/FOMC/ECB/BoE), plan, levels |
| Sunday | 📅 **GOLD — WEEK AHEAD** | DXY / real yields backdrop (FRED already wired), central-bank calendar, week plan, falsifier risks |
| drawdown guard trips | ⏸ **AUTO-PAUSED** | as today |

Formatting reuses `telegram.py` escaping/patterns (`format_*` siblings), sent
through the same bot/chat secrets.

## 9. File-level change plan (the follow-up PR)

| File | Change |
|---|---|
| `atrade/market_fx.py` | **new** — FX day boundary, sessions, thin holiday calendar |
| `atrade/broker.py` | MockBroker: XAUUSD symbol, spread model, oz quantities |
| `atrade/trading.py` | `check_stops()`, `trail_stops()`, `swing_size()`, exit grading |
| `atrade/engine.py` | `swing_open_run`, `risk_check_run`, `session_preview_run`, exit-event learning hooks |
| `atrade/config.py` | `xauusd` book block (risk %, ATR multiples, weekend policy, caps) |
| `atrade/telegram.py` | `format_swing_open`, `format_risk_check`, `format_exit` |
| `deploy/dispatch_fx.py` | **new** — windows from §4, tick-level stop enforcement, FX-day markers + catch-up |
| `.github/workflows/atrade_fx.yml` | **new** — near-24h cron; `atrade.yml` untouched |
| `state/state_fx.json` etc. | separate ledger/markers/mock account |

Explicitly out of scope until phase 2: live broker adapter, swap accounting,
instruments beyond XAUUSD (the book config is designed so XAGUSD/XAUUSD-style
pairs can be added as more `book` entries later).

## 10. Open questions (resolve during implementation)

1. Risk-check cadence: 2 reports/FX day (as designed) vs one per session block
   (4) — start with 2, the tick-level stop engine covers the gaps.
2. Price source for paper fills: `GC=F` (already fetched) vs `XAUUSD=X` —
   prefer `XAUUSD=X` for spread realism if the fetch is reliable.
3. `hold_with_tightened_stops` weekend mode — keep the knob, default flat.
4. Whether `swing_open_run` should be allowed to *add* to a position (pyramid)
   in phase 1 — default **no** (one entry per thesis, simpler grading).
