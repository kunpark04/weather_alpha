---
from: stats-ml-logic-reviewer
run_id: 20260530-0259
timestamp: 2026-05-30T04:05:00Z
scope_reviewed: market_wing+drop_lower_ask model-free flat-$2.50 fee-aware $25 backtest, +$7.98/96%WR claim for live deployment
verdict: partially sound
critical_flags: 0
warn_flags: 4
info_flags: 8
cross_references: []
note: Canonical full report is tasks/review_backtest_stats.md (per task instruction). This bus copy is a pointer + summary.
---

See the full structured audit at `tasks/review_backtest_stats.md`.

## Verdict: WEAK-BUT-DIRECTIONALLY-OK (deploy at $2.50 toy size only; do not scale on these numbers)
Sound where checkable and honestly caveated. Accounting exact (fees 22.6% of gross, single-count), reproduces to the cent (+$7.98, 23W/1L), no look-ahead, settlement parses 0/48 mismatch, fills realistically timed (staleness median 1 min), edge broad-based (top-3 wins = 21% of win dollars). Critically, the `drop_lower_ask` directional signal PASSES its placebo decisively: signal +$7.98/23W-1L vs placebo drop_higher_ask -$13.91/29W-18L (DD -69.9%). This is a genuine small edge, not an artifact. It is only WEAK on magnitude/generalization: the profit is front-loaded (early month +$7.11/15W-0L, late month +$0.87/8W-1L), breakeven WR is 0.836 vs observed 0.958 (3 bad days flip the window negative), one tuned knob is inert and the other sits at its PnL-max knee, and it's a single spring window from a large search. Run live at $2.50 as a forward test of fills + seasonality; do not scale capital on the +32%/2-month rate.

## Critical
- none. No issue invalidates the edge or loses money at $2.50 size. Concerns are magnitude/generalization, not validity.

## Warn
- W1: Edge is FRONT-LOADED. Clean OOS split: early +$7.11/15W-0L vs late +$0.87/8W-1L. Late (more-OOS) half barely clears zero.
- W2: Thin breakeven margin / one-sided tail. Breakeven WR 0.836 vs observed 0.958 (margin +0.12); one loss (-$2.27) ~= 5 wins; flip 3 median wins to losses -> -$0.21; bootstrap total CI [$1.50,$12.19], P(<=0)=1.1%.
- W3: In-sample knob behavior. max_ask_sum 0.90 is the PnL-max knee on this window; assumed_win_prob 0.92 is inert (0.90-0.99 plateau) and HANDOFF §1.5's claim for it is false.
- W4: Selection + synthetic +1c ask + spring-only N=24. Survivor of a large correlated search on its own window; +1c fill price unvalidated (quoteless tape); MAM only.

## Info
- I1/I2/I3: accounting exact, no look-ahead, settlement 0/48 mismatch (via parse_bucket), fills realistic (staleness median 1 min, max 8 min, all achievable ex-post).
- I4: PLACEBO PASSES decisively — drop_lower_ask +$7.98/23W-1L vs drop_higher_ask -$13.91/29W-18L (DD -69.9%); directional adjacency signal is real and load-bearing; market anchor mid-layout on 22/24 firing days.
- I5: fees 22.6% of gross; edge broad-based (top-3 wins = 21% of win dollars).
- I6: fee_aware guarantees only best-case-outcome non-negativity, not positive EV — correct narrow gate, not an EV filter.
- I7: Sharpe 5.17 = sqrt(252) over daily PnL incl. 37 zero no-trade days -> relative diagnostic only.
- I8: duplicate columns (day_pnl_cents, in_80pct_ci) in summary parquet -> downstream footgun, one-line fix.

## Top recommendations
1. Report the OOS-late result (+$0.87) and breakeven margin (0.836 vs 0.958) next to the +$7.98/96% headline so the front-loading and thin cushion are visible.
2. Stress fills at +2c/+3c before any capital scaling; use the orderbook logger's real depth to replace the +1c synthetic ask.
3. Drop the inert assumed_win_prob from the canonical command (or wire min_ev_margin>0); report max_ask_sum at 0.85/0.95 to expose the knee.
4. Require a positive net-of-fee result on >=1 non-spring month before scaling.
5. Re-publish the production-config placebo number (-$13.91) in HANDOFF as the standing evidence for the drop_lower_ask directional edge.

Audit-integrity note: an intermediate pass briefly (a) mis-measured fill staleness (~30% "stale"), (b) overstated fee load (~46%), and (c) wrongly concluded drop_lower_ask was a "dead branch" with an identical placebo — all caused by temp-file collisions during parallel knob sweeps, and all corrected via clean isolated re-runs. Final numbers above are from clean single-command runs: staleness median 1 min, fees 22.6%, placebo -$13.91 (signal strongly confirmed).
