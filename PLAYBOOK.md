# 📘 A-Trade Playbook — living trading rules & signal tracker

*Last updated: 2026-08-30T18:26:55+00:00*

This file is rewritten after every close run by the self-improvement loop. It is **not** a fixed rulebook — rules here are provisional hypotheses about what the market rewards, updated from evidence.

## How the loop works
1. Research → hypotheses with confidence (priors from this tracker).
2. Trade day (paper only).
3. Grade: was each hypothesis right? Which evidence mattered vs noise?
4. Update tracker → rewrite this file → next day's priors change.

## Current signal tracker (recency-weighted win rates)

| Signal category | n | Wins | Win rate | Recency w.r. | Edge $/trade | Priority | Trust |
|---|---|---|---|---|---|---|---|
| technical | 16 | 5 | 31.20% | 30.20% | -16.32 | -0.39 | HIGH |

**Trust levels** (anti-overfitting rule): **LOW** (<6 samples — treated as noise, prior heavily shrunk), **MED** (6–14 — partial weight), **HIGH** (15+ — full prior). Do not trust a category until it reaches HIGH.

## Rules in force (evolving)

1. Only trade hypotheses with confidence ≥ 60% (configurable).
2. Day trades only: every position opened at the open run is flattened at the close run.
3. Max 2 concurrent positions; max ~24% of equity deployed.
4. Intraday stop: ~1.4% adverse move → defensive close (stop_hit recorded).
5. Sizing scales with confidence: 60% → 6%, 70% → 9%, 80%+ → 12% of equity.
6. Pause trading after 6 consecutive runs with no composite-score improvement, or 3 consecutive failed measurements.
7. Benchmark = SPY buy & hold from the frozen baseline capture.

## Lessons learned this run

- Bootstrap: no graded trades yet — signal tracker is empty until the first close run produces outcomes.

## What mattered vs what was noise (evidence attribution)

- **technical**: when evidence direction agreed with the trade, avg P&L -74.94 vs +0.00 when it contradicted (3 agree / 0 contra). → evidence direction agreement favored contradiction by 74.94/trade.

## Discovered indicators / relationships

- technical: evidence direction agreement favored contradiction by 24.36/trade (n=3)
- technical: evidence direction agreement favored agreement by 20.15/trade (n=3)
- technical: evidence direction agreement favored contradiction by 37.87/trade (n=3)
- technical: evidence direction agreement favored agreement by 110.15/trade (n=3)
- technical: evidence direction agreement favored agreement by 47.61/trade (n=3)
- technical: evidence direction agreement favored contradiction by 56.82/trade (n=3)
- technical: evidence direction agreement favored contradiction by 63.70/trade (n=3)
- technical: evidence direction agreement favored contradiction by 74.94/trade (n=3)

## Open questions & falsifiable predictions

- Does 'macro' evidence (yields/CPI) beat 'news' noise in day trades? Tracker will tell us.
- Are high-confidence (≥0.75) setups worth the bigger size? Check win rate vs conf bucket.
- Short trades: do they lose more often in a liquidity-rich tape? Track separately.

---
⚠️ Paper trading only. No live orders are ever placed. This playbook is a research artifact, not investment advice.