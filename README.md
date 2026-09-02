# 🤖 A-Trade — Self-Improving Autonomous Paper-Trading Agent

A research-driven, self-improving day-trading agent that researches global markets every
trading day, forms **hypotheses with confidence scores**, executes **simulated trades on
Alpaca Paper Trading**, grades every trade as an experiment, and continuously rewrites its
own **living playbook** from what actually worked versus what was noise.

> ⚠️ **Paper trading only.** The code hard-codes Alpaca's paper endpoint
> (`paper-api.alpaca.markets`) and cannot send live orders. This is a research artifact,
> not investment advice.

---

## What it does every trading day

### 1. Research pass (open & close runs) — autonomous web data collection
| Signal | Source |
|---|---|
| Stock/ETF prices & bars (30-symbol universe) | Yahoo Finance v8 chart API (+ stooq fallback) |
| Bond yields (10Y/2Y), fed funds, unemployment, CPI, PPI | FRED CSV (cached once/day) |
| Recent SEC filings incl. **Form 4 insider activity**, 13D/13G, 8-K/10-Q/10-K | SEC EDGAR `data.sec.gov` |
| Earnings, Fed, CPI, oil, gold, AI-chip, market news headlines | Google News RSS (7 queries) |
| FX regime (EUR/GBP/JPY vs USD) | Frankfurter (ECB) |
| Analyst moves, earnings, geopolitics, sentiment, options-flow, historical analogs | `research/inbox.json` — human/AI researcher injects qualitative notes |

### 2. Hypothesis engine (the "brain")
Every research note becomes directional evidence. The engine aggregates evidence per
symbol into **hypotheses** with:
- **Thesis** (plain English) and **falsifiers** ("what would change my thesis")
- **Confidence score** combining: playbook priors (per signal-category win rates from the
  learning loop) + evidence strength + bull/bear agreement + freshness + technical confluence
- Only hypotheses with **confidence ≥ 60%** are tradeable

### 3. Execution (open run, 09:25 ET)
- Risk controls: max 2 positions, ≤24% of equity deployed, confidence-scaled sizing
  (60%→6%, 70%→9%, 80%→12%), ~1.4% intraday stop
- **Day trades only** — everything is flattened at the close run

### 4. Evaluation (close run, 15:50 ET) — graded by an independent evaluator
- **Composite score** = 35% portfolio return + 30% alpha vs SPY + 20% Sharpe + 15% win rate,
  normalized against a **frozen SPY baseline captured at setup**
- **Auto-pause** after 6 consecutive runs with no improvement, or 3 failed measurements

### 5. Self-improvement loop
- Grades every trade: hypothesis correct or refuted?
- **Evidence attribution**: for each signal category, does agreeing with its direction
  pay better than contradicting it? (matters vs noise)
- Updates the **signal tracker** (recency-weighted win rates per category) → these become
  next day's priors
- Rewrites **PLAYBOOK.md** with new rules, lessons, discoveries
- Logs **"why my thinking changed"** after every run

---

## Project layout

```
atrade/
├── atrade/
│   ├── engine.py       # run orchestration (open/close/status/resume)
│   ├── research.py     # autonomous data collection (Yahoo, FRED, SEC, RSS, FX, inbox)
│   ├── signals.py      # evidence → hypotheses with confidence + falsifiers
│   ├── trading.py      # risk sizing, order placement, close-out, stops
│   ├── evaluator.py    # composite score, SPY baseline, pause logic
│   ├── learning.py     # grading, signal tracker, playbook generation
│   ├── indicators.py   # RSI/SMA/momentum/volume confluence
│   ├── broker.py       # AlpacaPaper (paper) + MockBroker (offline dry-run)
│   ├── market.py       # US session calendar (2026 holidays, early closes)
│   ├── reporting.py    # plain-English run reports
│   ├── state.py        # persistent ledger/state
│   └── cli.py          # CLI + blocking watch scheduler
├── PLAYBOOK.md         # ⭐ living playbook — rewritten every close run
├── trades_ledger.json  # ⭐ every trade, hypothesis, confidence, outcome
├── research/inbox.json # ⭐ inject research notes here (analyst moves, sentiment…)
├── state/              # ledger, baselines, score history, reports, research cache
│   └── reports/        # open_YYYY-MM-DD.md, close_YYYY-MM-DD.md
├── .env.example        # Alpaca paper credentials template
└── run_*.sh            # helper scripts (open/close/watch)
```

`trades_ledger.json` is generated from `state/state.json` (`ledger` key) — run
`python3 -m atrade.cli report --run close` to view any day's report.

---

## Quick start

```bash
cd atrade
./setup.sh                  # creates .env, seeds PLAYBOOK.md
python3 -m atrade.cli status            # market state + next scheduled runs
python3 -m atrade.cli open              # research + open trades (09:25 ET style)
python3 -m atrade.cli close             # close day trades + evaluate + learn
./run_watch.sh             # blocking scheduler: runs at 09:25/15:50 ET on weekdays
```

### Enabling real Alpaca paper trading
1. Create a free paper account at <https://app.alpaca.markets/paper>
2. Copy `.env.example` → `.env` and paste your **PAPER** API key/secret
3. Without keys, the system auto-falls back to **MockBroker** (deterministic offline
   dry-run with simulated slippage and intraday drift) — safe for evaluating the loop.

### 📱 Telegram alerts (optional)
The bot messages you **four times every trading day** (all four are wired into the
scheduler — see `telegram_day_simulation.html` for a preview of every message):

| Time (ET) | Message |
|---|---|
| 09:25 | Open run — what it opened, today's watchlist with confidence + falsifiers |
| 10:30 | Mid-session check-in — open positions with unrealized P&L, events in play |
| 15:50 | Close run — trades + P&L, composite score, lessons, signal tracker, **tomorrow's watchlist** |
| 20:00 | Tomorrow preview — next trading day, calendar, prior-adjusted watchlist |
| Sun 17:00 | **Week-ahead digest** — next trading week, macro backdrop (yields/fed funds), calendar, week watchlist, falsifier risks |
| (on pause) | Auto-pause alert |

You can trigger any of them manually (e.g. to test):
```bash
python3 -m atrade.cli checkin    # send mid-session check-in now
python3 -m atrade.cli preview    # send tomorrow-preview now
python3 -m atrade.cli weekahead  # send week-ahead digest now
```

Setup:

1. In Telegram, chat with **@BotFather** → `/newbot` → copy the token.
2. Get your chat id: message your bot once, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the `"chat":{"id":...}` number.
3. Add both to `.env`:
   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=123456789
   ```
4. Test: `python3 -m atrade.cli notify`

The bot will also alert you if the agent auto-pauses. Skip the keys and it stays silent —
notifications are purely optional and never block a run.

**GitHub Actions:** `.env` is not available on the runner. Set the same two values as
repository secrets (`Settings → Secrets and variables → Actions`):

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Then verify with **Actions → atrade → Run workflow → "Send a test Telegram message now"**.
If the step fails, the log now prints the Bot API error (bad token, wrong chat id, never
messaged the bot, etc.) instead of going green with a silent no-op.

Telegram troubleshooting:
- You must message the bot at least once before it can message you back.
- Group chats have a **negative** chat id (e.g. `-100123...`).
- Empty / missing secrets used to skip every alert with only a DEBUG log line.

### 🔭 What does it scan? (it's not just AAPL/META)
Every run scans a **30-symbol universe** — the 2 demo trades were just the *positions
opened* on one day; scanning and trading caps are separate:

```
python3 -m atrade.cli universe      # show the current list
python3 -m atrade.cli universe "NVDA AMD META PLTR COIN SHOP WMT UNH ..."   # replace it
```
- `max_positions` (default 2) limits only how many trades are **opened** per day.
- The scanner covers every symbol in the universe **plus** FRED macro (yields, CPI, PPI,
  jobs, fed funds), SEC filings (insider Form 4s), FX, and 7 news RSS streams on every run.
- To extend SEC coverage to a new ticker, add it to `CIK_MAP` in `atrade/research.py`
  (AAPL, MSFT, NVDA, TSLA, AMD, META, AMZN, GOOGL, JPM, XOM, CVX, SPY, QQQ, IWM are built in).

**Does it take short trades? Yes.** Every hypothesis is either LONG or SHORT — when
bearish evidence dominates (bearish headlines, falling technicals, macro pressure), the
hypothesis side is `short`, opened with a sell and closed with a buy-to-cover (verified
end-to-end in the mock broker). Your Telegram watchlists show the direction of every
idea, e.g. `IWM SHORT — 73%`.

### Demo / testing
```bash
python3 -m atrade.cli open  --mock --anyday   # bypass weekday guard for demos
python3 -m atrade.cli close --mock --anyday
python3 -m atrade.cli sim --days 5            # fast multi-day sim on the mock broker
                                              # (no network — watch the playbook evolve)
python3 -m atrade.cli dashboard               # regenerate dashboard.html
python3 -m atrade.cli notify                  # send a Telegram test alert
```

### Starting fresh for live Alpaca paper
The simulation writes its own (mock) trade history. To start clean with real paper keys:
```bash
rm -rf state && python3 -m atrade.cli playbook
```
This resets the ledger, baseline, and score history — the SPY/QQQ baseline is then
re-captured on your first open run.

### Scheduling options
- `./run_watch.sh` — blocking loop, runs the sessions on weekdays + the Sunday digest
  (best under `nohup ... &` or a systemd unit)
- Cron: `0 9,10,15,20 * * 1-5` + `0 17 * * 0` with the CLI commands (the scripts themselves
  enforce weekday + holiday + pause guards)
- **Important:** a long-running process is not guaranteed to stay alive between sessions —
  a cron/task-scheduler is the reliable way to keep the cadence going.
- **GitHub Actions (free, recommended):** `deploy/dispatch.py` + the workflow handle all
  five windows (Sun 17:00 week-ahead; Mon–Fri 09:25/10:30/15:50/20:00) with once-per-day
  idempotency and DST-safe times — no VPS required.

---

## 🛡️ Overfitting & how long to run it

**Can it overfit?** Yes — the classic failure is trusting a signal category after a tiny
sample (e.g. "2 trades → 50%"). Three built-in guards mitigate this:

1. **Small-sample shrinkage** — a category's win rate barely moves your confidence until
   it has real history (n=5 → half weight, n=20+ → ~full weight). A 1-trade "win" changes
   almost nothing.
2. **Warm-up sizing** — the first 12 trades run at **40%→100%** of normal size, scaling up
   as the tracker accumulates evidence (`warmup_until_trades` / `warmup_min_factor` in
   `state/config.json`). Early learning can't cost meaningful size.
3. **Trust labels** — every signal category is labeled **LOW (<6 samples) / MED (6–14) /
   HIGH (15+)** in the playbook and reports. *Do not believe a learned edge until HIGH.*
   Plus recency-decayed win rates and grading against the frozen baseline (not in-sample fit).

**Suggested operating protocol (paper trading):**

| Phase | When | What happens | You do |
|---|---|---|---|
| 0 · Plumbing | days 1–2 | `sim --days 5` — verify the loop | nothing |
| 1 · Warm-up | ~first 12 trades (~2–3 weeks) | sizes at 40–100%, tracker fills up | nothing — just watch |
| 2 · Steady | next ~20 sessions | normal sizing | weekly: check tracker `n` values, score history, playbook rules |
| 3 · Review | first auto-pause, or ~6 weeks | the agent halts after 6 flat runs | review `PLAYBOOK.md` + reports, then `resume` |

**The auto-pause is not "graduation" and not failure** — it's a stagnation alarm: 6
consecutive close runs without a new best composite score (resets on any improvement),
or 3 failed measurements (missing benchmark data — rare). You'll see it coming via
`python3 -m atrade.cli status` → `Pause guard: no-improve streak X/6`. When it fires you
get a Telegram alert at ~16:00 ET that day. `python3 -m atrade.cli resume` restarts it;
nothing is erased.

**A note on the mock sim:** simulated results prove the plumbing works, not that the
strategy has edge. Real edge can only be judged on live paper data with HIGH-trust
sample sizes (15+ trades per category) — which is exactly what the warm-up phase is for.

## Interpreting the outputs

- **`PLAYBOOK.md`** — the living ruleset. Signal-tracker table shows each evidence
  category's win rate; the rules list evolves as the loop learns.
- **Run reports** — portfolio vs SPY/QQQ, positions, today's key events, active
  hypotheses + confidence, predictions, lessons, and "why my thinking changed".
- **Score history** — composite score per close run, with no-improvement streak for the
  auto-pause logic.
- **Agent log** — `state/logs/agent.log` for full debug/diagnostics.

## Configuration — trades, sizing & loss limits

Defaults live in `atrade/config.py`; the easy way to change them at runtime:

```bash
python3 -m atrade.cli config                    # show effective settings
python3 -m atrade.cli config max_positions 5    # set one value (persists to state/config.json)
python3 -m atrade.cli config reset              # back to defaults
```

Key knobs for testing:

| Setting | Default | What it does |
|---|---|---|
| `max_positions` | 2 | Max concurrent day trades **opened** per day (scanning is always all 30 symbols) |
| `max_position_pct` | 0.12 | Per-position size at 80%+ confidence (12% of equity) |
| `conf_to_size` | {0.60: 6%, 0.70: 9%, 0.80: 12%} | Sizing curve by confidence (interpolated) |
| `max_portfolio_pct` | 0.24 | Hard cap on total deployed (24% of equity) |
| `max_trade_value` | 20000 | Dollar cap per single trade |
| `intraday_stop_pct` | 0.014 | Per-position defensive stop (1.4% adverse) |
| `daily_loss_limit_pct` | 0.03 | Halts **new** trades for the day if today's P&L < −3% of equity |
| `max_drawdown_pct` | 0.10 | Auto-pauses if equity falls 10% below its running peak |
| `min_confidence` | 0.60 | Minimum hypothesis confidence to trade |

Example aggressive test preset (`python3 -m atrade.cli config` one at a time, or
copy `risk_presets.example.json` → `state/config.json`):
```
max_positions 5 · max_position_pct 0.15 · max_portfolio_pct 0.60 ·
max_trade_value 80000 · intraday_stop_pct 0.02 · daily_loss_limit_pct 0.04 · max_drawdown_pct 0.15
```
5 × 15% = 75% is capped by the 60% portfolio cap, so the portfolio cap binds first —
intentional. On GitHub, create/edit `state/config.json` in the repo (it persists across
runs via the commit-back step).

## Dynamic universe (Tier 2) — the scan grows with the market

The base 30-symbol watchlist is the floor, not the ceiling. Every research pass also
sweeps a **~60-symbol candidate pool** and adds names with real, current evidence:

| Setting | Default | What it does |
|---|---|---|
| `auto_dynamic_universe` | true | Master switch for discovery + rotation |
| `max_dynamic_additions` | 12 | Max names added to the scan set per run |
| `momentum_top_n` | 6 | How many top momentum/volume names are considered |
| `rotation_bias_top` | 3 | Top sector ETFs whose symbols get a confidence bump |
| `min_dynamic_volume` | 500000 | Liquidity floor (avg 5d $ volume) for additions |
| `max_per_cluster` | 2 | Max concurrent positions per correlated cluster (semis, big-tech, …) |

How it works, in order:
1. **News/SEC discovery** — RSS news + SEC Form 4 filings are scanned for candidate-pool
   tickers (`extract_ticker_mentions`); mentioned names join the scan.
2. **Momentum rotation** — candidates ranked by 5-day return (volume as tiebreak);
   top names join the scan (`momentum_rotation`).
3. **Sector rotation** — sector ETFs ranked by 5-day return; the top 3 are stored and
   their symbols get a +3pp confidence bump in `build_hypotheses`.
4. **Correlation cap** — when opening, positions are grouped into `correlation_clusters`
   (semis, bigtech_software, consumer_tech, financials, energy, …); a cluster can hold at
   most `max_per_cluster` positions, so a basket of 5 longs can't silently become 5 chip
   stocks. Blocked names are logged with the reason (`cluster 'semis' already at 2 positions`).
5. **Per-symbol learning** — every closed trade is graded per **symbol** (not just per
   category) in the signal tracker; the playbook shows each symbol's n / wins / win rate /
   edge and flags `⭐ KEEP` (≥60% win rate) or `⚠️ DEMOTE` (≤35%). Demoted symbols lose
   their per-symbol bonus. All of it is capped by `max_dynamic_additions` and the 60%
   confidence gate — additions are *scanned*, only traded if a ≥60% hypothesis forms.

The dynamic additions appear in Telegram (`dynamic adds today: …`) and in the open report
(`## Dynamic universe additions`). Watch the report's scan size to see it grow: the more
news/momentum names it finds, the wider the net — and the correlation cap keeps the
portfolio diversified.

## Safety rails
- Paper endpoint hard-coded; live endpoints not implemented.
- No leverage; shorts allowed but sized the same as longs and only when bearish evidence
  dominates; day-trade-only so no overnight risk accumulates.
- Auto-pause on stagnation/failed measurements; manual `resume` command.
- All network failures are logged and skipped — research is best-effort, never fatal.

---

*Built for educational research on simulated markets. Past performance in the paper
environment says nothing about future results — this is an experiment harness, not a
money-making system.*
