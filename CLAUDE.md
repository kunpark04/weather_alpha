# CLAUDE.md — Forecast Alpha project guide

Architectural and behavioral guide for Claude working in this repo. Detailed
results, retrain steps, and reproducible-command snippets live in [`HANDOFF.md`](HANDOFF.md).

---

## 1. What this project is

A probabilistic ML forecaster + production trading engine for **Kalshi `KXHIGHCHI`**,
the daily maximum-temperature market at **KMDW** (Chicago Midway). The model
outputs a calibrated PMF over integer °F at 1 PM local; the engine maps that PMF
onto the day's 6 Kalshi buckets and decides whether and how to bet.

**Mode:** paper trading. Live mode is wired but not yet enabled.

---

## 2. Architecture

```
notebooks/model_v3.ipynb            <-- model R&D, retraining, calibration
        |
        v  saved artifacts (joblib + npy + parquet)
data/model_v3_artifacts/
        |
        v  loaded by
forecast_alpha/                     <-- production package (always-on bot)
   model.py        Prediction dataclass + artifact loader
   pmf.py          PMF utilities, bucket parsing, rearrangement
   kalshi.py      KalshiContract dataclass, status filter
   live_fetchers.py     async METAR/TAF/ASOS/HRRR/CLI/Kalshi
   strategy.py    8 strategy fns; run_wing_strategy is production
   calibration.py LOO alpha (currently 0 -- market-only)
   fees.py        Kalshi fee = ceil(7% * N * P * (1-P))
   execution.py   paper/live order routing
   engine.py      orchestrator (data refresh / anchor / intraday)
   scheduler.py   minute-tick scheduler
   tui.py         Textual TUI
   main.py        entry point
        |
        v  driven by
config/forecast_alpha.yaml          <-- persistent knobs (bankroll, kelly, throttle)
```

Three orthogonal layers: **model** (notebook + artifacts), **strategy/execution**
(`forecast_alpha/`), **config** (YAML). Strategies are pure functions of
`(StrategyCfg, Prediction, contracts, feature_row, bankroll)` → `StrategyOutput`.

---

## 3. Core domain concepts

- **KXHIGHCHI buckets per event:** four middle 2°F-wide buckets (e.g. `72-73`, `74-75`)
  plus two open-ended tails (`<=71`, `>=80`). Six contracts total. Strikes float
  per event around the day's expected high — NOT a fixed grid.
- **Model PMF:** 1°F-wide integer grid from -30 to 130 (161 bins). Bucket
  probability is computed by integrating the PMF over the bucket's F range.
- **Wing:** a coverage trade — modal Kalshi bucket + both positional adjacents (3 legs),
  optionally reduced to 2 legs via `drop_lower_ask` / `drop_higher_ask` / `drop_worst_leg`.
- **Wing anchor:** which bucket is at the wing's center. Default `model_modal`
  (highest model integrated prob). Alternative `market_modal` (highest yes_ask).
- **Agreement day:** model_modal_ticker == market_modal_ticker. 56% of days; ±1 bucket
  Top-3 hit rate is **100%** on these days.
- **Equal-payout sizing:** `stake_i = K × ask_i` so each leg pays K dollars on win,
  regardless of which leg wins.
- **Kelly + throttle:** quarter-Kelly multiplied by a throttle in {1.0, 0.5, 0.25}
  that halves once per active condition (low model confidence; large HRRR-vs-persistence gap).

---

## 4. Behavioral guidelines (don't repeat past mistakes)

1. **Always account for fees.** Kalshi fee = `ceil(7% × N × P × (1-P))`. Apply at
   entry. Never report PnL gross-of-fees.
2. **Trust the market over the model on per-bucket prob.** LOO α-blend converges to
   α=0; the market is empirically more informative (Brier 0.43 vs 0.69; Kalshi
   Top-1 64% vs 44%). Don't build strategies that assume model > market on per-bucket
   edge — they lose money (`joint_kelly`, `two_bucket_arb`, `variance`, `hrrr_bias`,
   `regime_confident` all failed for this reason).
3. **Always run a placebo when claiming a directional edge.** For `drop_lower_ask`
   we ran `drop_higher_ask` and it failed at -$431, confirming signal. Without the
   placebo, the +$308 result could have been lottery.
4. **Sample-size discipline.** N<20 backtest days = no verdict. The 91% win rate
   on `wing + drop_lower_ask` (22 days) is suggestive, not conclusive. Future
   variants need OOS-style time-split validation before claiming edge.
5. **Don't hide filters.** Any new gate must be documented in `HANDOFF.md` §3 and
   the strategy's docstring. The "hidden filter" critique is a real one.
6. **HRRR features are already in °F.** `hrrr_t2m_max_peak` is converted in
   `model_v3.ipynb` §3. Do NOT re-apply K→F conversion downstream.
7. **`event_date` parsing: prefer the ticker pattern** (e.g. `KXHIGHCHI-26MAY24-...`).
   Kalshi's API `settlement_date` is strike_date + 1 day — off-by-one.
8. **Bankroll compounds across backtest days.** Loss magnitudes on later days
   reflect the grown bankroll, not larger per-day risk fractions.
9. **Match scope to ask.** When asked to test a variant, test that variant. Don't
   silently tune `max_ask_sum` or other parameters without flagging it.
10. **CLAUDE.md and HANDOFF.md are not duplicates.** CLAUDE.md is architecture +
    behavior. HANDOFF.md is durable hand-off — full reproducible commands,
    detailed result tables, open items. Keep them aligned but distinct.

---

## 5. Conventions

- **Working directory** is the project root (`forecast-alpha/`). All relative paths
  in scripts assume this.
- **Times** are tz-aware. Local = `America/Chicago`. Anchor = 1 PM local
  = `13:00 CT` = `18:00 UTC` (CST) / `19:00 UTC` (CDT).
- **Units** are °F throughout (model + features + truth). Internal HRRR data was
  K originally; conversion happens once in `model_v3.ipynb` §3.
- **Bucket spec strings:** `"72-73"` (inclusive range), `"<=71"` (lower tail),
  `">=80"` (upper tail). Parsed by `forecast_alpha/pmf.py:parse_kalshi_subtitle`.
- **Strategy CLI flags** use `--wing-*` prefix and kebab-case (e.g. `--wing-drop-lower-ask`).
- **Diagnostic field names** in strategy output: `sum_asks`, `p_top_wing`, `p_used`,
  `ev_margin`, `agreement`, `wing_anchor`, etc. (post-rename, no legacy `_3` suffixes).

---

## 6. Strategy results summary (one-line per variant)

Full details + reproducible commands in [`HANDOFF.md`](HANDOFF.md) §1 and §6.
> **Production pivoted 2026-05-29 → `market_wing + drop_lower_ask`, flat-$ sizing, MODEL-FREE.**
> The table below is the original Kelly @ $1000 comparison; under flat-$2.50 on the real $25
> account, `market_wing` is +$7.98 / +32% / 96% WR / −7% DD. The v3 model is **not used** by
> `market_wing` (anchor=market, p_used=assumed_win_prob). See [`HANDOFF.md`](HANDOFF.md) §1.5.

On 67-day Kalshi window (2026-03-21 → 2026-05-26), $1000 starting bankroll, quarter-Kelly:

| # | Strategy | Days | WR% | PnL | Sharpe | Max DD |
|---|---|---|---|---|---|---|
| 1 | **`wing + drop_lower_ask`** (PRODUCTION) | 22 | **91%** | +$308 | **+2.85** | **-$181** |
| 2 | `market_wing + drop_lower_ask` | 40 | 88% | +$371 | +1.66 | -$369 |
| 3 | `wing_any + drop_lower_ask` (no agreement) | 41 | 78% | +$330 | +0.64 | -$1090 |
| 4 | `wing + drop_worst_leg` | 11 | 73% | +$278 | +1.31 | -$445 |
| 5 | `wing` prob-weighted | 10 | 60% | +$156 | +1.57 | -$162 |
| 6 | `wing` baseline (3-leg eq-payout) | 10 | 60% | +$40 | +3.87 | -$2 |
| — | `wing + drop_higher_ask` (PLACEBO) | 26 | 65% | -$431 | -1.55 | -$919 |
| — | `joint_kelly`, `variance`, `hrrr_bias`, `regime_confident` | various | — | all negative | — | — |

---

## 7. Workflow rules

- **Plan first** for any task that spans 3+ files or makes architectural choices.
- **Always run a backtest** before claiming a strategy improvement. Use
  `scripts/backtest_strategy.py` with explicit CLI flags — don't tune in-place.
- **Use existing scripts** in `scripts/` rather than re-implementing logic:
  - `scripts/all_variants_retest.py` — full variant comparison
  - `scripts/verify_bucket_accuracy.py` — bucket-level model vs market accuracy
  - `scripts/sizing_breakdown.py` — per-day stake decomposition
- **Modify `forecast_alpha/strategy.py` only with intent.** Function signatures
  are stable (named kwargs, no positional args after `*`). Renames cascade to
  `scripts/backtest_strategy.py` CLI flags and to 5+ analysis scripts.
- **The notebook `model_v3.ipynb` is the single source of truth for retraining.**
  §3.* cells are dynamic-exec'd by `live_predict.ipynb`. Renaming a §3 cell header
  breaks live prediction.

---

## 8. Managerial doc index

Every md file that governs how this project is worked on:

| File | Role |
|---|---|
| [`README.md`](README.md) | Project entry point — current state, folder overview, quick start |
| [`HANDOFF.md`](HANDOFF.md) | Durable hand-off — full result tables, sizing pipeline, open items, reproducible commands |
| [`README_PROD.md`](README_PROD.md) | Production engine (`forecast_alpha/`) architecture, paper-mode setup, going-live checklist |
| [`Language and Architecture Choices for a Production-Grade Kalshi Trading Bot in the Terminal.md`](Language%20and%20Architecture%20Choices%20for%20a%20Production-Grade%20Kalshi%20Trading%20Bot%20in%20the%20Terminal.md) | Research doc — Python/C++/Rust/Go trade-offs, TUI framework choices, Kalshi SDK landscape |
| `notebooks/model_v3.ipynb` | Production model notebook — features, CV, calibration, §10.3 Kalshi-resolution diagnostic |
| `notebooks/model_v4.ipynb` | Parallel R&D variant — midnight anchor (NOT production) |
| `notebooks/live_predict.ipynb` | Live prediction pipeline (loads v3 artifacts) |
| `config/forecast_alpha.yaml` | Persistent strategy/risk config |
| [`deploy/README.md`](deploy/README.md) | Always-on deployment guide — orderbook-logger systemd unit + hosting options |

Excluded from the index (auto-generated or vendored, no managerial role):
`__pycache__/`, `archive/` (legacy notebooks/scripts), `logs/`, `data/*.parquet`.

**Hygiene rule:** when adding any managerial md file (decision log, runbook, lessons),
also add a row to this table in the same commit.
