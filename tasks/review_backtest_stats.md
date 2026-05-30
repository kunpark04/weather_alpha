# Statistical / Methodological Audit — `market_wing + drop_lower_ask` production backtest

**Reviewer:** stats-ml-logic-reviewer (independent, adversarial)
**Date:** 2026-05-30
**Scope:** The live-deployment claim `market_wing + drop_lower_ask`, model-free, flat-$2.50/trade, fee-aware, $25 bankroll → "24 fires, 23W/1L (96% WR), +$7.98 (+32%), -6.7% max DD" over 2026-03-22 → 2026-05-21 (61 settled days).
**Repro command (verified reproduces to the cent):**
`python scripts/backtest_strategy.py --strategy market_wing --wing-drop-lower-ask --wing-assumed-win-prob 0.92 --wing-max-ask-sum 0.90 --wing-flat-usd 2.5 --wing-fee-aware --bankroll 25`
→ 24 fires, 48 legs, 23W/1L, gross +$10.31, fees $2.33, **net +$7.98**, DD -6.7%, Sharpe 5.17.

---

## VERDICT: WEAK-BUT-DIRECTIONALLY-OK (deploy at $2.50 toy size only; do NOT scale on these numbers)

The result is **sound where it can be checked and is honestly caveated.** The accounting is exact, the result reproduces to the cent, there is no look-ahead, settlement parses correctly, fills are realistically timed, and — importantly — **the `drop_lower_ask` directional signal passes its own placebo test decisively** (signal +$7.98/23W-1L vs placebo `drop_higher_ask` -$13.91/29W-18L, DD -69.9%). The edge is broad-based, not outlier-driven, and the sign survives a temporal split. This is a genuine small edge, not an artifact.

It is only *weak* — and must stay at the stated toy size — for four reasons, all of which are about generalization and margin, not correctness:

1. **The edge is heavily front-loaded in time.** Clean OOS split: early month (Mar22–Apr21) **+$7.11, 15W/0L**; late month (Apr22–May21) **+$0.87, 8W/1L**. The later, more-out-of-sample half barely cleared zero. Almost the entire profit comes from the first month of a 2-month window. That is the single biggest yellow flag.
2. **Thin margin over breakeven + one-sided tail.** Mean win-day +$0.45, the single loss day -$2.27 (one loss ≈ 5 wins). Breakeven win rate = **0.836**; observed 0.958 → margin only +0.12. **Flipping 3 median-win days to -$2.27 losses turns the 2-month result negative (-$0.21).** Bootstrap 95% CI for total PnL is wide: **[$1.50, $12.19]**, P(total≤0) ≈ 1.1%.
3. **One tuned knob is inert; the other sits at its knee.** `assumed_win_prob` is flat from 0.90–0.99 (0.88 drops to 21 fires/+$7.56) — 0.92 is in the inert plateau. `max_ask_sum` binds *below* 0.90 (0.80→+$4.22/11 fires; 0.85→+$6.71/17; 0.90→+$7.98/24; 0.95→+$6.48/29) — so the headline 0.90 is exactly the value that maximizes fires and PnL on this window. Mild in-sample knob-fitting.
4. **Selection / single spring window.** Survivor of a large correlated search (8 strategies, 12+ wing variants, anchor + sizing sweeps) reported on the same 2 spring (MAM) months it was selected on; no multiple-testing correction; no non-spring data.

Bottom line: this is a credible coverage-wing edge with a *validated* directional component, but the magnitude rests on the first of two spring months, the breakeven margin is slim, and the +1¢ fill assumption is untested live. Run it at $2.50 as a **forward test of fills and seasonality.** Do not scale capital, and quote the breakeven margin (0.836 vs 0.958) and the OOS-late result (+$0.87) alongside the headline so the thinness is visible.

---

## Methodological Audit

- **Choice of evaluation:** single-window historical replay, 61 settled days, strategy + knobs chosen after seeing the window. Script CAVEAT #1 correctly calls the edge an upper bound; CAVEAT #5 explicitly asks for a held-out OOS split. Good discipline. The HANDOFF §0 deployment framing drops the upper-bound caveat — recommend restoring it.
- **Win-rate as headline metric — over-sells the margin.** For an equal-payout 2-leg wing, WR is mechanically high (≥1 leg usually wins) and is a poor EV proxy. The load-bearing quantity is net-of-fee $/day vs the breakeven WR: breakeven 0.836, observed 0.958, margin +0.12. "96%" reads as a huge safety cushion; the real cushion is 3 bad days.
- **Placebo test — PASSES DECISIVELY (verified, corrects an earlier error in this audit).** Repo rule (CLAUDE.md §4.3): run a symmetric placebo before claiming a directional edge. In the exact production config: signal `drop_lower_ask` = +$7.98 (24 fires, 23W/1L); placebo `drop_higher_ask` = **-$13.91 (47 fires, 29W/18L, Sharpe -2.59, DD -69.9%)** — it loses badly. The strong asymmetry confirms the market's adjacency-direction signal is real and load-bearing in the shipped strategy, mirroring the original model-wing result (placebo there lost -$431). The market-modal anchor sits mid-layout on 22/24 firing days (edge on only 2), so both adjacents exist and the higher-ask-vs-lower-ask choice is a genuine, exercised decision. [Note: an intermediate pass of this audit wrongly concluded the placebo was a "dead branch" with identical PnL, due to a temp-file collision during parallel runs; re-run cleanly in isolation, the placebo gate is satisfied with margin.]
- **Knob sensitivity (swept cleanly):** `assumed_win_prob ∈ {0.88..0.99}`: 0.88→21 fires/+$7.56, 0.90–0.99→24 fires/+$7.98 (plateau). It feeds bypassed-Kelly + the `ev_margin≥0` gate, which clears at 0.90+. `max_ask_sum ∈ {0.80..0.97}`: monotone up to 0.90 then declines (looser admits more fee-marginal days). 0.90 is the PnL-maximizing knee on this window.
- **Alternatives the original analysis did not execute (I did):** net-of-fee $/day with CI; breakeven-WR comparison; the production-config placebo; the OOS time-split (HANDOFF TODO). All reported here.

---

## Data Audit

- **`data/kalshi_history.parquet`:** 446,027 rows, 67 events, 2026-03-21 → 2026-05-26. Columns: `event_ticker, event_date, ticker, subtitle, settlement_value, strike_type, floor_strike, timestamp, yes_price_cents, no_price_cents, count, taker_side`. **No bid/ask/orderbook columns** — this is a *trade tape*. `settlement_value` is a string (`'yes'/'no'`) = the taker side of each trade, NOT the official market resolution; do not use it as settlement ground truth.
- **Fill model:** `snapshot_at_anchor` takes the last trade ≤ 1 PM anchor and sets `yes_ask = last_trade + 1¢` (verified: 39/48 entries exactly last+1; the rest differ only by the 0.99 clamp/rounding). The +1¢ half-spread is a modeling assumption unvalidatable from a quoteless tape (HANDOFF P2 is correctly open).
- **Staleness — GOOD.** Entry-price staleness (anchor − last pre-anchor trade) median **1 min**, p90 **3 min**, max **8 min** on firing days; 0 fills off a >60-min-stale trade; all 48 legs have a post-anchor trade at/below the entry price, i.e. a fill at the assumed price was achievable ex-post. The 1 PM book on firing days is liquid enough that the synthetic ask is anchored to a fresh trade.
- **Leakage — PASS.** `snapshot_at_anchor` filters `timestamp <= anchor_t_utc`. With no exit rule (production default) `simulate_position` settles immediately from CLI truth and never consults post-anchor trades for entry; post-anchor trades are read only when intraday exit rules are active (they are not). The model is not used by `market_wing` (anchor=market, p_used=assumed_win_prob), so model/OOF leakage is moot.
- **Settlement parsing — PASS.** Independent re-evaluation of `parse_bucket(bucket_spec)(actual_f)` vs the backtest `won` flag: **0/48 mismatches.** Printout is self-consistent (actual 76°F → `75-76` won; actual 71°F → `>=71` won). No off-by-one bucket/date error in this window. (Confirms internal consistency against CLI truth; not cross-checked against Kalshi's official resolution, which is not reliably in this file.)
- **Representativeness:** 2 months, all spring (MAM) — the season the repo flags as hardest and where the market's intraday-sharpening edge is strongest. Summer/winter book behavior unobserved. Regime risk real and unquantified.

---

## Result Interpretation

- **Accounting (verified):** fee recomputed from `ceil(7%·N·P·(1-P))` matches stored fees exactly (max diff 0¢); applied once per leg at entry, not double-counted; stored `net == payout − cost − fee` exactly (max diff 0¢). 23 days with exactly 1 winning leg, 1 day (2026-05-12) with 0 winning legs (-$2.27). Every firing day is 2 legs.
- **Fee load:** gross +$10.31, fees $2.33 → **fees = 22.6% of gross**, 4.6% of stake. Net edge ≈ 8.9% per dollar staked (gross ≈ 11.4%). Material but not dominant.
- **Fee-aware gate semantics — note the wording.** `fee_aware` skips a fire only when fees would eat the *best-case* win (`best_win_payout − total_cost − total_fee ≤ 0`). It guarantees the trade *can* profit on its single best outcome; it does NOT guarantee positive EV. That is the correct, narrow check for "don't trade a structurally dead day," but it should not be read as an EV filter. Ablation shows it adds +$2.20 and halves DD by gating ~2–3 fee-marginal days.
- **Statistical significance (day level):** WR 23/24 = 0.958, Wilson 95% CI ≈ [0.79, 0.99]. **Breakeven day-WR = 0.836** (mean win +$0.448, loss -$2.27). Bootstrap of the 24 daily PnLs: total +$7.98, **95% CI [$1.50, $12.19]**, **P(total≤0) ≈ 1.1%**. Per-day edge +$0.333, sd $0.592.
- **What the bootstrap does NOT capture:** the +1¢ synthetic-ask assumption, fee/fill drift, the time-decay of the edge (front-loaded), and out-of-window regime shift. A ~1% in-sample p-value on one spring window is not "99% safe live."
- **Practical significance / fragility:** +$7.98 on $25 over 2 months = +32%, but $0.33/trade-day on ~$2.11 mean daily stake. **Sensitivity: flip 1 median win to a -$2.27 loss → +$5.25; flip 2 → +$2.52; flip 3 → -$0.21.** Three extra bad days erase the result; the loss is one-sided and ~5× a typical win.
- **Concentration — GOOD:** top-3 win days are 21% of total win dollars; broad-based across 23 wins, not propped up by outliers.
- **Multiple-testing:** survivor of a large correlated search on the selection window; no FWER/FDR control. Treat +$7.98 as the max of many correlated trials.
- **Component ablation (flat-$2.5, awp0.92, mas0.90):**
  - drop_lower_ask + fee_aware (PROD): 24 fires, +$7.98, DD -6.7%, Sharpe 5.17
  - **drop_higher_ask + fee_aware (PLACEBO): 47 fires, -$13.91, DD -69.9%** → signal strongly confirmed
  - → `fee_aware` and the 2-leg `drop_lower_ask` together produce the edge; the *direction* of the drop is load-bearing (placebo loses badly and fires ~2× as often).
- **OOS temporal split (clean):** Early Mar22–Apr21 **+$7.11, 15W/0L**; Late Apr22–May21 **+$0.87, 8W/1L**. Sign stable but magnitude collapses out-of-sample — the edge is front-loaded.

**Licenses you to conclude:** a 2-leg market-modal coverage wing with a fee gate and a *validated* higher-ask-adjacent selection was net-positive across two spring months, with a real directional component (placebo passes) and a broad win base. **Does NOT license:** (a) that the magnitude generalizes — it's front-loaded, late-month barely positive; (b) that the knobs are doing what HANDOFF §1.5 claims (awp inert at 0.92, mas tuned to its knee); (c) that the gross edge survives worse-than-+1¢ fills (untested); (d) that 96% WR implies a wide margin (breakeven 0.836; 3 bad days flip it); (e) generalization beyond spring or beyond toy size.

---

## Impact on Project Scope

- **If the result stands:** justified to run the model-free engine live at $2.50/trade as a **forward test of the three things the backtest cannot validate — fill realism (+1¢), edge persistence over time (front-loading), and non-spring seasonality.** The orderbook logger (HANDOFF L1/P2) is the right next instrument; it replaces the synthetic ask with real depth.
- **If fragile (it partly is):** premature to (1) scale stake, (2) trust the +32%/2-month rate forward (the late month says ~+$0.87/month), (3) rely on the tuned knob values. Hold until live fills + ≥1 non-spring month are observed.
- **Consistency with prior findings:** CONFIRMS the market-anchored coverage wing is net-positive, that the `drop_lower_ask` adjacency signal is real (placebo passes here as it did for model-wing), and that `fee_aware` does real work (HANDOFF §1.5). COMPLICATES the magnitude story: §0's +$7.98/+32% is front-loaded; the durable rate looks closer to the late-month +$0.87. CONTRADICTS §1.5's claim that `assumed_win_prob 0.92` is doing work (inert plateau); PARTIALLY CONTRADICTS the `max_ask_sum 0.90` claim (it binds only below 0.90 and is tuned to its PnL-max knee).

---

## Recommended Alternatives (ranked by information value)

1. **Report the OOS-late and breakeven margin next to the headline, not just +$7.98/96%.** Add "early month +$7.11/15W-0L, late month +$0.87/8W-1L; breakeven WR 0.836 vs observed 0.958; 3 bad days flip the window negative." *Answers:* does the edge persist and how much cushion is there? *Why better:* the headline implies a durable +32%/2mo and a huge WR cushion; both overstate. *Decision rule:* if the trailing-month net-of-fee $/day trends to 0, pause before scaling.
2. **Stress the fill model: re-price entries at +2¢ and +3¢.** *Answers:* does an 8.9% net edge survive a worse spread on an illiquid book? *Why better:* +1¢ is the largest unvalidated assumption and the edge is thin. *Decision rule:* if +2–3¢ turns the late month or the full window negative, the edge is inside the spread → data-collection size only.
3. **Use the orderbook logger's real depth to replace the synthetic ask before any capital scaling** (HANDOFF L1/P2). *Decision rule:* gate any stake increase on ≥N days of real-fill reconciliation showing realized cost ≤ assumed cost.
4. **Drop or wire the inert knob; report mas neighbors.** `assumed_win_prob` is a no-op in the 0.90–0.99 plateau — either remove `--wing-assumed-win-prob` from the canonical command or set `min_ev_margin>0` so it gates. Report `max_ask_sum` at 0.85 and 0.95 next to 0.90 so the knee-tuning is visible.
5. **Require a positive net-of-fee result on ≥1 non-spring month (forward-collected) before scaling.** Two spring months cannot speak to summer/winter book behavior.
6. **Re-publish the production-config placebo number in HANDOFF (-$13.91) as the standing evidence for the directional edge.** It is the cleanest single justification for `drop_lower_ask` and currently lives only in this audit.

---

## Severity Flags

**CRITICAL**
- (none) — no issue found that invalidates the edge claim or would lose money at the stated $2.50 size. The accounting, leakage, settlement, fills, and placebo all check out. The concerns below are about magnitude/generalization, not validity.

**WARN (materially weakens confidence in the magnitude / scaling)**
- W1. **Edge is front-loaded:** clean OOS split gives early +$7.11/15W-0L vs late +$0.87/8W-1L. Almost all profit is in the first of two spring months; the more-OOS half barely clears zero. *(verified)*
- W2. **Thin breakeven margin / one-sided tail:** breakeven WR 0.836 vs observed 0.958 (margin +0.12); one loss (-$2.27) ≈ 5 wins; flipping 3 median wins to losses → -$0.21; bootstrap CI [$1.50, $12.19]. 96% WR does not imply a safe margin. *(verified)*
- W3. **In-sample knob behavior:** `max_ask_sum 0.90` is the PnL-maximizing knee on this window; `assumed_win_prob 0.92` is inert (0.90–0.99 plateau) and HANDOFF §1.5's mechanistic claim for it is false. *(verified by sweep)*
- W4. **Selection + synthetic +1¢ ask + spring-only N=24:** survivor of a large correlated search on its own window; the +1¢ fill price is unvalidated (no quotes in data); 24 fires (15/9 per OOS half), MAM only. *(verified)*

**INFO**
- I1. Accounting correct: fee formula exact, applied once per leg, no double-count; stored net == payout−cost−fee exactly. *(verified)*
- I2. No look-ahead in the production path; settlement logic parses correctly (0/48 bucket-predicate mismatches vs `won`). *(verified)*
- I3. Fill timing realistic: entry-trade staleness median 1 min, max 8 min; all 48 fills achievable ex-post at the assumed price. *(verified)*
- I4. **Placebo PASSES decisively:** `drop_lower_ask` +$7.98/23W-1L vs `drop_higher_ask` -$13.91/29W-18L (DD -69.9%) — the directional adjacency signal is real and load-bearing; market anchor is mid-layout on 22/24 firing days. *(verified; corrects an intermediate error in this audit)*
- I5. Fees = 22.6% of gross ($2.33/$10.31); edge broad-based (top-3 wins = 21% of win dollars). *(verified)*
- I6. `fee_aware` guarantees only best-case-outcome non-negativity, not positive EV — correct narrow gate, do not read as an EV filter. *(code review)*
- I7. Sharpe 5.17 is √252-annualized over daily PnL incl. 37 zero no-trade days, which deflate mean and std — a relative diagnostic only, not a tradeable annualized Sharpe.
- I8. Duplicate columns (`day_pnl_cents`, `in_80pct_ci`) in the summary parquet — harmless to the printout but a downstream footgun (caused a transient miscount during this audit). One-line fix in `backtest()` row assembly.
