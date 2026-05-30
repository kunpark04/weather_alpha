# Weather Alpha — Handoff (KMDW / Kalshi KXHIGHCHI)

_Last updated: 2026-05-29_

Probabilistic ML model + production trading engine for **Kalshi KXHIGHCHI**
(daily max temperature at **KMDW**, Chicago Midway). Model anchored at T = 1 PM
local; output is a calibrated PMF over integer °F, mapped to Kalshi's 6-contract
bucket structure for edge computation. **Currently in paper-trading mode.**

---

## 0. Current state at a glance

> ### PRODUCTION STRATEGY (paper) — pivoted 2026-05-29
> **`market_wing` + `drop_lower_ask`, flat-$ sizing — MODEL-FREE.** Anchors on the
> market modal (the v3 model is *not* used — see §1.5), keeps the higher-ask adjacent,
> sizes a flat **$2.50/trade** on the real **$25** Kalshi account. Now wired into the
> engine (config dispatch, replacing the old `joint_kelly` default) and validated
> end-to-end in paper on live data. Backtest @ $25 (flat-$2.50, `max_ask_sum 0.90`,
> fee-aware): **24 fires, 96% WR, +$7.98 (+32%), −7% max DD**.
>
> _Prior candidate (Kelly, model-anchored):_ `wing + drop_lower_ask` — +$308 / 91% /
> Sharpe 2.85 / −12.5% on $1000. Higher Sharpe, but flat sizing brings market_wing's
> drawdown to ~parity while keeping more fires, and market_wing needs no model →
> far simpler live path. Sizing rationale + caveat: memory `project_sizing_flat_dollar`.

> ### PRODUCTION MODEL
> **`model_v3.ipynb`** — T = 1 PM anchor, 46 features (incl. HRRR path-5).
> Artifacts at `data/model_v3_artifacts/`. CRPS 1.204 °F; Top-1 bucket 45.8 %;
> Top-3 (±1 bucket) 87.8 % over 1,966 OOF days (2021-01-01 → 2026-05-21).

| Item | Value |
|---|---|
| Best strategy | `wing` + `drop_lower_ask` (agreement-required) |
| Win rate | 91 % (20/2 day-level over 22 fires) |
| Net PnL | +$308 on $1000 starting bankroll |
| Sharpe (annualized) | +2.85 |
| Max drawdown | -$181 (-18%) |
| Best alternative | `market_wing` + `drop_lower_ask` (+$371 on 40 days, but Sharpe +1.66 / max DD -$369) |
| Best model | model_v3 (1 PM anchor) |
| Model CRPS | 1.204 °F |
| Top-1 / Top-3 bucket | 45.8 % / 87.8 % (2°F bins, 5-yr OOF) |
| Kalshi-resolution Top-3 on agreement days | **100 %** (61-day window) |
| OOF window | 2021-01-01 → 2026-05-21 (1,966 days) |
| Kalshi history | 2026-03-21 → 2026-05-26 (67 days) |

---

## 1. Strategy verdict

### 1.1 Top-level strategies tested (8 dispatched via `--strategy <name>`)

| Strategy | Verdict |
|---|---|
| `joint_kelly` (default per-bucket Kelly) | ❌ -$795 / 12% WR — model has no per-bucket edge vs market (Brier 0.69 vs 0.43; LOO α = 0) |
| `two_bucket_arb` | ❌ same root cause as joint_kelly |
| `wing` (3-leg agreement-required) | ✅ baseline +$40 / 60% WR; **+drop_lower_ask** is the production winner |
| `wing_any` (no agreement) | ❌ -$149 indiscriminately; agreement filter is load-bearing |
| `market_wing` (market-anchor wing) | ⚠ profitable with `drop_lower_ask` (+$371) but 2× the drawdown of `wing` |
| `variance` | ❌ -$366 (1/9 W/L) — dispersion signal is wrong direction |
| `hrrr_bias` | ❌ -$457 (0/9 — never wins) |
| `regime_confident` | ❌ no profitable parameter exists in [peak_p × hrrr_gap] grid |
| ~~`hard_floor`~~ | dropped per current direction |

### 1.2 Wing variant matrix (parameter sweeps within `--strategy wing`)

| Variant | Days | W/L | WR% | PnL | Sharpe | Max DD |
|---|---|---|---|---|---|---|
| **`wing` + `drop_lower_ask`** (PRODUCTION) | 22 | 20/2 | **91%** | **+$308** | **+2.85** | **-$181** |
| `wing` + `drop_worst_leg` | 11 | 8/3 | 73% | +$278 | +1.31 | -$445 |
| `wing` prob-weighted | 10 | 6/4 | 60% | +$156 | +1.57 | -$162 |
| `wing` market-weighted (pow=5) | 8 | 6/2 | 75% | +$84 | +0.71 | -$168 |
| `wing` eq-payout (loose, max=1.00) | 10 | 6/4 | 60% | +$40 | +3.87 | -$2 |
| `wing` eq-payout (strict, max=0.97) | 7 | 6/1 | 86% | +$39 | +4.37 | $0 |
| `wing` + `drop_higher_ask` (PLACEBO) | 26 | 17/9 | 65% | -$431 | -1.55 | -$919 |
| `market_wing` + `drop_lower_ask` | 40 | 35/5 | 88% | +$371 | +1.66 | -$369 |
| `wing_any` + `drop_lower_ask` | 41 | 32/9 | 78% | +$330 | +0.64 | -$1090 |

### 1.3 Why `wing + drop_lower_ask` works

- **Wing structure**: cover model's modal Kalshi bucket + both positional adjacents → ±1 bucket coverage.
- **Agreement filter** (`require_agreement=True`): only fire when model_modal == market_modal. On agreement days (34/61), **Top-3 hit rate is 100%** at Kalshi-bucket resolution.
- **`drop_lower_ask` transform**: keep modal + the higher-ask of the two adjacents. The market is empirically 82% accurate at picking which adjacent to bet on (confirmed by placebo: `drop_higher_ask` loses $431 in the opposite direction).
- **Equal-payout sizing**: `stake_i = K × ask_i` so payout on any winning leg = K dollars regardless of which won.

### 1.4 Midnight anchor (v4 model) — tested 2026-05-28, NOT viable

Tested whether the wing edge survives at a **midnight forecast anchor** (model
`v4`, midnight market snapshot) instead of 1 PM. It does not — on either axis.

**Model side (v4 midnight OOF):** far weaker than v3 at bucket prediction.

| Metric | v3 (1 PM) | v4 (midnight) |
|---|---|---|
| §10.2 fixed-2°F Top-1 / Top-3 (5-yr OOF) | 45.8% / 87.8% | 19.1% / 48.7% |
| Kalshi-resolution model Top-1 / Top-3 | 44.3% / 91.8% | 15.2% / 27.3% |
| Agreement with market modal | ~56% | ~15–18% |

Even at **equal information** (both at midnight), the market beats v4 decisively
(market Top-3 90.9% vs v4 27.3%). The market's edge is an intraday-information
effect: its Top-1 sharpens 40.9% (midnight) → 62–64% (1 PM) and its 2-leg
coverage 69.7% → 95.5%. At midnight the book is still diffuse.

**Strategy side:** both top-2 strategies flip to losing on the midnight anchor
(identical params; only model→v4, anchor→0):

| Strategy | 1 PM (v3) | midnight (v4) |
|---|---|---|
| `wing + drop_lower_ask` | +$308 (20/2) | **−$125** (7/2, 9 fires) |
| `market_wing + drop_lower_ask` | +$371 (35/5) | **−$388** (43/20) |

`wing` fires rarely (9 days — v4 rarely agrees with the market) and over-bets
(it carries `assumed_win_prob=0.99`, true at 1 PM where the wing settles 94–100%
but false at midnight where it settles ~83%). `market_wing` fires often but the
diffuse midnight book means a 2-leg wing covers only ~70% at ~the same cost →
68% WR, −71% max DD. **Conclusion: the edge lives in the 1 PM information
environment; the midnight anchor is not tradeable.** Repro scripts in §6.

**Mechanism (quantified, `scripts/v3_v4_why.py`):** v4's PMF is ~2.4× wider than
v3's (CRPS 2.85 vs 1.20 °F; 80% CI 14.3 vs 5.9 °F; peak prob 14.8% vs 31.2%)
because a midnight anchor has (a) no same-day observations — by 1 PM the daily
high is already realized on **58% of days** — (b) no 12Z HRRR forecast (not yet
published at midnight; v4 has zero HRRR features), and (c) no running-max hard
floor. The gap is worst in spring (MAM CRPS 2.7× v3) — the Kalshi window.

**Tracked:** v4's complete artifact set is committed at `data/model_v4_artifacts/`
(parallel to v3) for the record — it is **not** production.

### 1.5 Production pivot: market_wing, flat-$, model-free (2026-05-29)

`market_wing + drop_lower_ask` (market anchor, no agreement) is the production
candidate. Established this session:

- **The v3 model is NOT used by market_wing.** Verified in code: anchor = market
  modal, `require_agreement=False`, `p_used = assumed_win_prob` (overrides the model
  PMF), `equal_payout` split, regime throttle bypassed under flat sizing. The PMF
  only feeds diagnostics. → the **same-day weather anchor (old P3) is obsolete for
  production** — the engine runs market-only, no weather, no model, no creds.
- **Sizing: flat-$, not Kelly.** Kelly@0.99 was an ~8× over-bet (its only size
  variation came from the throttle, which mis-fired on the 2026-04-16 tail). The
  `assumed_win_prob` sweep showed 0.92 halves the drawdown for ~90% of PnL; flat
  sizing ~tripled Sharpe at parity PnL/DD. Flat-$ ($2.50) chosen for the $25
  non-compounding account. **Caveat** (memory `project_sizing_flat_dollar`): flat-$
  re-risks during drawdowns and is in-sample-WR-dependent; **flat-% is the principled
  long-run choice** — revisit when scaling capital.
- **$25 scale validated (Phase 0):** net-positive at every flat-$ level; a fee-aware
  gate + `max_ask_sum 0.90` trim the ~10% fee-dead high-cost days (fee drag ~4% vs
  2.6% at $1000).
- **Engine wiring:** `ModelCfg.enabled` (config `false`) gates `run_cycle` to skip
  refresh/features/predict and use a uniform placeholder PMF; `_dispatch_strategy`
  routes `strategy.name` → `run_wing_strategy`. Replaces the hardcoded `joint_kelly`
  (a known loser). flat-$ + fee-aware added to `run_wing_strategy`.

---

## 2. Sizing & risk pipeline

```
stake_per_day = f_stake × bankroll
where  f_stake = min(kelly_full × kelly_fraction × throttle, total_exposure_max_pct)
       kelly_full ≈ assumed_win_prob − (1−assumed_win_prob) × sum_asks/(1−sum_asks)
       throttle ∈ {1.0, 0.5, 0.25}    (halve once per active condition)
```

| Param | Value | Source |
|---|---|---|
| `bankroll_usd` | $1000 (paper) | `config/weather_alpha.yaml` |
| `kelly_fraction` | 0.25 (quarter-Kelly) | config |
| `assumed_win_prob` | 0.99 | CLI `--wing-assumed-win-prob` |
| `max_ask_sum` | 1.00 | CLI `--wing-max-ask-sum` |
| `per_contract_max_pct` | 0.20 ($200 per leg cap) | config |
| `total_exposure_max_pct` | 0.60 | config |
| `regime_throttle.confidence_floor_top1` | 0.20 (halve if peak_P below) | config |
| `regime_throttle.hrrr_persistence_gap_max_f` | 8 °F (halve if exceeded) | config |
| `assumed_spread_cents` | 2 (sim ask = mid + 1¢) | CLI |

**Throttle is doing real work:** the second-worst loss day (2026-05-12, -$180) was already halved by the HRRR-persistence-gap throttle (gap = 23 °F). Without it, the loss would have been ~-$360.

**The unavoidable tail (1 day per ~22 trades):** when truth lands outside the bought wing on a "calm" day where both model confidence and HRRR-persistence agree, no current gate stops it. Worst observed: 2026-04-16, -$181 in `wing + drop_lower_ask` (-$368 in `market_wing` due to larger compounded bankroll).

---

## 3. Pipeline / execution order (every gate, no hidden filters)

| # | Stage | What | Skip reason if failed |
|---|---|---|---|
| 1 | Backtest loop | `oof_fold >= 0`, no NaN PMF, `date in cli_truth`, snapshot non-empty | data pre-filters |
| 2 | Wing | ≥3 live Kalshi contracts | "need >= 3 live contracts" |
| 3 | Wing | Identify model_modal (argmax bucket_prob) and market_modal (argmax yes_ask) | (step, not a gate) |
| 4 | Wing | **`require_agreement`** | "model-market disagreement" |
| 5 | Wing | Build wing = [anchor, pos-1, pos+1] (anchor = model_modal by default) | (step) |
| 6 | Wing | `len(wing) >= min_wing_size` (2 if any drop_X, else 3) | "modal at edge of layout" |
| 7 | Wing | **`drop_lower_ask`** transform (keep modal + higher-ask adj) | (transform) |
| 8 | Wing | `sum_asks < max_ask_sum` | "sum_asks >= max" |
| 9 | Wing | `ev_margin = assumed_win_prob - sum_asks >= min_ev_margin` | "EV margin < min" |
| 10 | Wing | Regime throttle (halve × halve) | scales sizing, never blocks fire |
| 11 | Wing | Kelly + caps (`per_contract_max_pct`, `total_exposure_max_pct`) | "kelly <= 0" |
| 12 | Wing | Per-leg integer rounding, ≥2 legs after caps | "only N legs after caps" |
| 13 | Settlement | `won = bucket_contains(actual_F, spec)`, fee = `ceil(7% × n × p × (1-p))` | — |

---

## 4. Model side (carry-over)

Self-contained notebook: fetch → features → walk-forward CV → calibration →
deployable ensemble → evaluation → artifact save.

- **§3 Feature engineering** — 46 features across 7 groups (astro, climatology+persistence, TAF peak, METAR @ T, ASOS, HRRR base+path-5, hard-boundary running max).
- **§5 Walk-forward CV** — monthly test folds, 1-day gap, ≥12-month min train, 3 seeds × 19 quantiles.
- **§6 Quantile → PMF** — Chernozhukov rearrangement, integer-°F grid.
- **§8 Calibration** — season-selective isotonic (JJA only; DJF/MAM/SON pass through raw).
- **§9 Deployable ensemble** — final models on all rows, packaged in `data/model_v3_artifacts/`.
- **§10.2 Bucket accuracy** — 2°F fixed-pair, model-only. 45.8% / 87.8%.
- **§10.3 NEW: Kalshi-resolution accuracy** — model vs market modal, split by agreement. Added 2026-05-28.

Key §10.3 findings (2026-Q2, n=61):

| Subset | Model Top-1 | Market Top-1 | Model Top-3 | Market Top-3 |
|---|---|---|---|---|
| All days | 44.3 % | **63.9 %** | 91.8 % | **98.4 %** |
| Agreement (n=34) | 67.6 % | 67.6 % | **100.0 %** | **100.0 %** |
| Disagreement (n=27) | 14.8 % | 59.3 % | 81.5 % | 96.3 % |

→ The market beats the model unconditionally. The wing strategy works because on agreement days both signals point the same direction; on disagreement days the model is catastrophic (14.8% Top-1) so skipping them is correct.

### Retraining
`model_v3.ipynb`, **Run All** (~21 min). Final cell saves to `data/model_v3_artifacts/`.

---

## 5. Production code (weather_alpha package)

| Module | Role |
|---|---|
| `weather_alpha/strategy.py` | All 8 strategy functions; `run_wing_strategy` is the production code path |
| `weather_alpha/calibration.py` | LOO α blending (model-market); currently α=0 (market-only) per LOO fit |
| `weather_alpha/live_fetchers.py` | Async live data (METAR/TAF/ASOS/HRRR/CLI) for the running bot |
| `weather_alpha/engine.py` | Production engine orchestrator; `verify_bankroll` (LIVE balance) + `_tradeable_contracts` event guard |
| `weather_alpha/tui.py` | Textual TUI for live monitoring |
| `weather_alpha/main.py` | Entry point |
| `weather_alpha/scheduler.py` | Anchor-aware loop; state persisted to `data/scheduler_state.json` across restart |
| `weather_alpha/pmf.py` | Bucket parsing, PMF utilities, Chernozhukov rearrangement |
| `weather_alpha/fees.py` | Kalshi fee formula |
| `weather_alpha/kalshi.py` | `KalshiContract` dataclass |
| `weather_alpha/model.py` | Artifact loader, `Prediction` dataclass |
| `weather_alpha/config.py` | YAML config loader |
| `config/weather_alpha.yaml` | Persistent config (bankroll, kelly_fraction, throttle, etc.) |

---

## 6. Reproducing the production backtest

```powershell
python scripts/backtest_strategy.py `
    --strategy wing `
    --wing-drop-lower-ask `
    --wing-assumed-win-prob 0.99 `
    --wing-max-ask-sum 1.00 `
    --out data/prod_backtest.parquet
```

Expected: 22 fires, +$308 PnL, 20/2 W/L. Positions land in `data/prod_backtest_positions.parquet`.

### Other reproducible scripts
- `scripts/all_variants_retest.py` — full 12-variant comparison
- `scripts/market_wing_test.py` — 3 market_wing variants
- `scripts/drop_lower_ask_oos.py` — 3-part OOS validation (early/late/placebo)
- `scripts/sizing_breakdown.py` — per-day stake decomposition
- `scripts/verify_bucket_accuracy.py` — §10.2 verification + §10.3 computation
- `scripts/full_67day_backtest.py` — filtered vs unfiltered comparison on full Kalshi window
- `scripts/v3_v4_kalshi_compare.py` — v3-vs-v4 model & market accuracy at Kalshi resolution, both anchors (§1.4)
- `scripts/wing_settlement.py` — 1/2/3-leg wing settlement % by model & anchor hour (§1.4)
- `scripts/market_modal_coverage.py` — market top-1/2/3 modal coverage, midnight vs 1 PM (§1.4)
- `scripts/v3_v4_why.py` — quantifies the v3-vs-v4 skill gap (CRPS, sharpness) + information mechanism (§1.4)
- `scripts/backtest_strategy.py --model-dir <dir> --anchor-hour-local <H>` — backtest any model dir at any anchor hour; OOF-only mode if the dir lacks deployable artifacts

---

## 7. Open items

| # | Task | Status / Notes |
|---|---|---|
| P1 | **Wire production strategy into the engine** | ✅ DONE — config dispatch → `market_wing + drop_lower_ask` + flat-$ sizing, replacing `joint_kelly`. Model-free path (`ModelCfg.enabled=false`) verified end-to-end in paper on live data. |
| L1 | **Deploy the orderbook logger always-on** | `scripts/orderbook_logger.py` on a VM/Pi via `deploy/orderbook-logger.service` (see `deploy/README.md`). Keyless. Survives laptop shutdown + auto-restarts. |
| L0 | **Order-safety hardening (pre-live review)** | ✅ DONE 2026-05-30. Engine+stats review (`tasks/review_engine_logic.md`, `tasks/review_backtest_stats.md`). All 4 CRITICALs fixed: C1 LIVE fill-confirmation (book actual fills via `get_positions`, not assumed), C4 deterministic `client_order_id` (no dup orders on retry), C3 per-leg kill-switch + intraday outlay breaker (`Book.daily_outlay_cents` vs `daily_max_loss_usd`) + `per_anchor_max_trades`. Kill switch: `python scripts/kill.py` (arm/disarm/status). Verified: `scripts/check_live_execution.py`. **Stats verdict: edge real (placebo passes) but thin + front-loaded → $2.50 toy forward-test only, don't scale.** |
| L2 | **Go live** | Add Kalshi RSA creds (`KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH`); confirm with read-only `scripts/check_kalshi_auth.py`; set `mode: live`; run the engine at the 1 PM anchor on an always-on host. On activation the engine verifies the real bankroll from `get_balance()` — config `$25` is only a hint. |
| L3 | **Deeper reconciliation (post-first-trade)** | ✅ Mostly DONE 2026-05-30 (commits `1c6b679`, `292fd5d`): bankroll verified from `get_balance` at activation + re-synced after settlement (W1/W5); scheduler state persisted across restart (W2); event-selection guard (W3); per-leg `place_order` error handling (W4). **Still TODO:** reconcile the Book against `get_positions()` at *cycle start* (book out-of-band / mid-exception fills — the C1/W5 tail) + a write-ahead order-intent log (I1); needs a live authenticated account to exercise. |
| P2 | **Liquidity verification** | Does Kalshi fill at the assumed `mid + 1¢`? The orderbook logger's depth ladders now answer this directly from collected data. |
| ~~P3~~ | ~~Same-day live anchor~~ | **OBSOLETE for production** — market_wing is model-free (§1.5), so no same-day weather feed is needed. (Free real-time sources verified anyway: NWS `metar_substitute` / AviationWeather are token-free; precision = the hourly METAR T-group.) |
| P4 | **Tail-risk** | Largely addressed by flat-$ sizing (no over-bet); the 04-16-type tail is now capped at the flat stake. |
| P5 | **Expose flat-$ in the backtest harness** | The +$7.98/$25 production figure uses flat-$ + fee-aware sizing that lives only in `run_wing_strategy` / engine config — `backtest_strategy.py` has no `--wing-flat-usd` / `--wing-fee-aware` flag, so the headline number was an inline analysis. Add the flags so it reproduces from one command. |
| cleanup | `data/hrrr_12z_KMDW_legacy.parquet` | 9-var pre-path-5 snapshot, deletable |

---

## 8. Constants

```
STATION              = KMDW
LOCAL_TZ             = America/Chicago
T_HOUR_LOCAL         = 13 (v3)
PEAK_WINDOW          = 12-16 local
HRRR_INIT_HOUR_UTC   = 12
HRRR_GRID            = (iy=665, ix=1169)
SEEDS                = [42, 43, 44]
QUANTILES            = 0.05..0.95 step 0.05 (19 total)
INTEGER_F_GRID       = arange(-30, 131)         (161 bins, 1°F wide)
KALSHI BUCKETS       = 4×2°F middles + 2 open-ended tails (per event)
USE_CALIB_FOR_SEASONS = {"JJA"}
HAS_HARD_FLOOR        = True (v3 only)
```

---

## 9. Key data files

### 9.1 Source / model data

| File | Contents | Window |
|---|---|---|
| `data/kalshi_history.parquet` | All KXHIGHCHI trades + quotes | 2026-03-21 → 2026-05-26 (67 events) |
| `data/cli_KMDW.parquet` | NWS CLI daily max truth | 2015-01-01 → present |
| `data/model_v3_artifacts/oof_bucket_probs_calib.npy` | OOF PMFs, shape (2332, 161) | 2021-2026 |
| `data/model_v3_artifacts/feature_df.parquet` | 46-feature snapshot per OOF row | |
| `data/model_v3_artifacts/target_df.parquet` | `cli_high` per date | |
| `data/model_v3_artifacts/final_models.joblib` | Deployable ensemble | last trained 2026-05-23 |
| `data/live_log.parquet` | Per-contract prediction log (paper-mode) | growing |

### 9.2 Cached backtest results

Each backtest run produces two parquets: `<tag>.parquet` is the daily summary
(`day_pnl_cents`, `n_targets`, etc.) and `<tag>_positions.parquet` is the per-leg
detail (`date`, `ticker`, `bucket_spec`, `contracts`, `stake_cents`, `fee_cents`,
`net_cents`, `won`, `p_model`, `p_market`, `kelly_f`, `bankroll_pre`, ...).

**Profitable variants — current snapshots:**

| # | Strategy | File stem |
|---|---|---|
| 1 | `wing + drop_lower_ask` (PRODUCTION) | `data/retest_wing_droplow` |
| 2 | `market_wing + drop_lower_ask` | `data/mw_droplow` |
| 3 | `wing_any + drop_lower_ask` | `data/retest_wingany_droplow` |
| 4 | `wing + drop_worst_leg` | `data/retest_wing_dropworst` |
| 5 | `wing` prob-weighted | `data/retest_wing_prob` |
| 6 | `wing` market-weighted (pow=5) | `data/retest_wing_mkt5` |
| 7 | `wing` eq-payout (loose, max=1.00) | `data/retest_wing_loose` |
| 8 | `wing` eq-payout (strict, max=0.97) | `data/retest_wing_strict` |

**Negative variants (kept for placebo / failure-mode reference):**

| Strategy | File stem |
|---|---|
| `joint_kelly` | `data/retest_joint_kelly` |
| `wing + drop_higher_ask` (PLACEBO) | `data/retest_wing_drophigh` |
| `wing` market-weighted (pow=3) | `data/retest_wing_mkt3` |
| `wing_any` (no agreement) | `data/retest_wingany` |
| `wing_any + drop_higher_ask` (PLACEBO) | `data/retest_wingany_drophigh` |
| `variance` | `data/retest_variance` |
| `hrrr_bias` | `data/retest_hrrr_bias` |
| `regime_confident` | `data/retest_regime_conf` |
| `market_wing` baseline (3-leg) | `data/mw_baseline` |
| `market_wing + drop_higher_ask` (PLACEBO) | `data/mw_drophigh` |
| `market_wing + drop_lower_ask` + agreement (== production) | `data/mw2_market_agree` |
| `wing + drop_lower_ask` (re-run for comparison) | `data/mw2_model_agree` |

Quick read:
```python
import pandas as pd
pos = pd.read_parquet("data/retest_wing_droplow_positions.parquet")
```

**Regenerate all:**
```powershell
python scripts/all_variants_retest.py          # 12 variants
python scripts/market_wing_test.py             # 3 market_wing variants
python scripts/market_wing_diag.py             # market_wing + agreement comparison
```

**Stale-cache risk:** these files are NOT regenerated automatically. Re-run if
the model is retrained, new Kalshi data is pulled, or any wing-strategy code
changes. The numbers in the §1 tables of this hand-off were computed from the
current snapshots.

---

## 10. Conventions

- Wing **anchor** = the bucket the wing is centered on. Default is `model_modal`. `--wing-anchor market` switches to `market_modal` (used by `market_wing` dispatcher).
- **Equal-payout sizing**: per-leg stake = K × yes_ask, where K = total_stake / sum_asks. After integer rounding, n_contracts ≈ K on every leg, so the gross payout on any winning leg = K dollars.
- **Agreement day** = day where model_modal_ticker == market_modal_ticker. Empirically 56% of days; 100% Top-3 hit rate at Kalshi resolution.
- The Kalshi day-event has six contracts (4 middle 2°F buckets + 2 open-ended tails), not five and not 3°F-wide. Modal-at-edge happens when model picks one of the two tails.
