# Forecast Alpha — Handoff (KMDW / Kalshi KXHIGHCHI)

_Last updated: 2026-05-27_

Probabilistic ML model predicting the NWS CLI daily-maximum temperature at
**KMDW (Chicago Midway)** for trading Kalshi's **KXHIGHCHI** daily-high market.
Output is a calibrated PMF over integer °F, mapped to Kalshi's 6-contract
bucket structure for edge computation.

---

## 0. Current state at a glance

> ### ⭐ PRODUCTION MODEL (use this)
> **`model_v3.ipynb`** — T = 1 PM local anchor, **46 features (incl. HRRR path-5)**.
> - **Artifacts last trained:** `2026-05-23 22:11` → `data/model_v3_artifacts/`
> - This is what `live_predict.ipynb` loads when `MODEL = "v3"`.
> - Config in `constants.json`: 46 features, `USE_CALIB_FOR_SEASONS=["JJA"]`, `HAS_HARD_FLOOR=True`.
> - **`model_v4.ipynb` is NOT production** — it's the midnight-anchor R&D variant,
>   has no HRRR features yet, and has no saved artifacts. Do not use for live trading.
>
> **To confirm the loaded model is current:** `live_predict.ipynb` §1 prints
> `model='v3'`, `46 cols`, `hard_floor=True`, `calib_seasons=['JJA']`. If feature
> count ≠ 46 or the artifact date is stale relative to `model_v3.ipynb`'s mtime,
> re-run `model_v3.ipynb` end-to-end (the last cell re-saves artifacts).

| Item | Value |
|---|---|
| Best / production model | **model_v3** (T = 1 PM local anchor) |
| Artifacts trained | 2026-05-23 22:11 |
| Features | **46** (40 base + 6 HRRR path-5) |
| Ensemble | 3 seeds × 19 quantiles, LightGBM quantile regression |
| Monotonicity | Chernozhukov rearrangement on quantile crossings |
| CV | Walk-forward, monthly test folds, 1-day gap |
| Calibration | Season-selective isotonic — **JJA only** (DJF/MAM/SON pass through raw) |
| Hard floor | v3 post-hoc projection: zero P(F < `running_max_F_T_hard`), renorm |
| **CRPS (raw)** | **1.220 °F** |
| **CRPS (season-selective, shipped)** | **1.204 °F** |
| Top-1 bucket accuracy | 45.8 % |
| Top-3 (±1 bucket) | 87.8 % |
| OOF days evaluated | 1,966 (2021-01-01 → 2026-05-21) |
| Baseline (handoff start) | CRPS 1.406 °F → **−14.4 %** improvement |

Trajectory: pre-HRRR 1.406 → +HRRR base 1.201 → +path-5 1.204. The model has
plateaued near a ~1.20 °F CRPS floor; remaining variance is largely irreducible
daily-weather noise given information available at T = 1 PM.

---

## 1. Backtest pipeline — `model_v3.ipynb`

Self-contained notebook: fetch → features → walk-forward CV → calibration →
deployable ensemble → evaluation → artifact save.

### 1.1 Sections

| § | Purpose |
|---|---|
| §1 | Setup, config, data fetchers, parquet-cached load |
| §2 | Target construction (CLI `max_temp_f` → `cli_high`, local-day boundary) |
| §3 | Feature engineering (§3.1–§3.8; see below) |
| §4 | Walk-forward CV splitter (monthly, 1-day gap, ≥12-month min train) |
| §5 | Quantile model training (3 seeds × 19 quantiles); OOF prediction loop |
| §6 | Quantile → integer-°F PMF (rearrangement + interpolation + ±0.5 bucketing) |
| §7 | Evaluation — CRPS, empirical baselines, PIT, sharpness, per-month skill |
| §8 | Isotonic recalibration (§8.1 fit, §8.2 apply season-selective, §8.3–§8.6 eval) |
| §9 | Deployable final ensemble (trained on all rows) + `predict()` / `predict_summary()` |
| §10 | Kalshi-bucket prediction + visualization + bucket-accuracy diagnostic |
| (tail) | 2026-05-20 prediction smoke test; **artifact-save cell** (run last) |

### 1.2 Feature inventory (46)

**Astronomical (5)** — date-only, deterministic seasonal forcing:
`doy_sin`, `doy_cos`, `solar_elevation_noon`, `daylight_hours`, `t_hours_after_sunrise`

**Climatology + persistence (4)** — leakage-safe lags:
`year`, `cli_high_yesterday`, `cli_high_lag_mean_3d`, `cli_high_rolling_5yr`
(5-yr same-DOY rolling mean, current year excluded via shift)

**TAF peak-window (7)** — latest issuance ≤ T, time-weighted over noon–4 PM:
`taf_ovc_frac_peak`, `taf_drct_sin_peak`, `taf_drct_cos_peak`, `taf_sknt_peak`,
`taf_vis_sm_peak`, `taf_precip_frac_peak`, `taf_minutes_since_issue`

**METAR state at T (9)** — latest METAR with local_hour < 13, all temps °F:
`metar_slp_mb_T`, `metar_temp_f_T`, `metar_dewp_f_T`, `metar_wdir_sin_T`,
`metar_wdir_cos_T`, `metar_wspd_kt_T`, `metar_vis_sm_T`, `metar_slp_tendency_6h`,
`metar_dewp_depression_f_T`

**ASOS (3)** — 1-min granularity, local midnight → T:
`asos_min_temp_f_overnight`, `running_max_F_T`, `asos_temp_f_T`

**HRRR base (11)** — 12Z run, peak window (fxx≈5–10, DST-aware):
`hrrr_t2m_max_peak`, `hrrr_t2m_mean_peak`, `hrrr_t2m_min_peak`, `hrrr_d2m_at_peak`,
`hrrr_tcc_mean_peak`, `hrrr_cape_max_peak`, `hrrr_lftx_min_peak`, `hrrr_wind_mean_peak`,
`hrrr_wind_dir_sin_at_peak`, `hrrr_wind_dir_cos_at_peak`, `hrrr_mslma_mean_peak`

**HRRR path-5 (6)** — second-pass backfill (radiation, snow, mixing):
`hrrr_dswrf_mean_peak`, `hrrr_dswrf_max_peak`, `hrrr_sde_max_peak`,
`hrrr_sdwe_max_peak`, `hrrr_blh_mean_peak`, `hrrr_blh_at_peak`

**v3 hard-boundary (1)** — strict floor through CLI rounding chain:
`running_max_F_T_hard` (METAR T-group running max, 0.1 °C → °F → round to whole °F)

### 1.3 Key design decisions (and the why)

- **Season-selective calibration (JJA only).** Per §8.6 diagnostic: isotonic
  recalibration wins CRPS in summer (−0.07 °F JJA) but loses in DJF/SON
  (over-widens already-wide winter PMFs to chase perfect PIT). Net win by
  calibrating JJA only.
- **HRRR −2 °F mean bias is load-bearing.** The model learns to correct HRRR
  upward by ~2 °F on average. This wins most days but misfires on sharp
  regime-change days (e.g., 2026-05-20: HRRR forecast 60.6 °F, actual 61 °F,
  model predicted 62–63 °F because of the learned +2 °F correction).
- **Anomaly / disagreement features were tried and reverted.** Both
  `cli_high_anomaly_*` and `hrrr_minus_persistence` features marginally HURT
  CRPS for a small Top-1 gain. LightGBM lacks enough regime-change training
  examples (~3 % of days) to learn context-dependent feature weighting.
- **Path-5 features kept despite ~flat CRPS** (+0.003 °F vs base HRRR) for the
  +1.5 pp Top-1 lift. Snow signal is sparse (~8 % of days) so it can't move the
  aggregate much, but it's physically correct and helps DJF marginally.

### 1.4 Retraining

Open `model_v3.ipynb`, **Run All** (~21 min: §5 walk-forward ≈16 min, §9 final
ensemble ≈20 s, rest fast). The last cell saves to `data/model_v3_artifacts/`:
- `final_models.joblib` — list[3 seeds][19 quantiles] of LightGBM boosters
- `isotonic_recalibrators.joblib` — {DJF, MAM, JJA, SON} IsotonicRegression
- `constants.json` — FEATURE_COLS, QUANTILES, INTEGER_F_GRID, SEASON_OF_MONTH,
  T_HOUR_LOCAL, HAS_HARD_FLOOR, USE_CALIB_FOR_SEASONS, etc.
- `feature_df.parquet`, `target_df.parquet` — reference snapshots
- `oof_bucket_probs_{raw,calib}.npy`, `oof_fold.npy` — OOF diagnostics

**Gotcha:** notebooks import `from metar import Metar` in §1, so they cannot be
executed via `nbconvert` unless that kernel has the `metar` package. Run
interactively (your IDE kernel has it).

### 1.5 model_v4 (parallel variant)

`model_v4.ipynb` — T = **midnight** local anchor (predicts day D using D-1 data).
Differences from v3: `T_HOUR_LOCAL=0`, METAR/derived temps in °C (not °F),
**no `running_max_F_T_hard`** (at midnight it's a persistence proxy, not a strict
floor), `HAS_HARD_FLOOR=False`. **v4 has no HRRR features yet** — the 12Z run
isn't available at midnight; v4 needs the 00Z backfill (see §6, outstanding P2).

---

## 2. Live pipeline — `live_predict.ipynb`

21 cells, multi-model (`MODEL = "v3"` switch in §1). Loads persisted artifacts —
no retraining at predict time (~seconds end-to-end after the data refresh).

### 2.1 Sections

| § | Cell(s) | Purpose |
|---|---|---|
| §1 | 2 | Pick MODEL, load artifacts; errors loudly if `HAS_HARD_FLOOR` missing |
| §2 | 4 | Refresh IEM (`refresh_data.py`) + **HRRR catch-up** (`backfill_hrrr.py`); HRRR-aware anchor selection; build target_df |
| §3 | 6 | **Dynamic-exec** of `model_{MODEL}.ipynb`'s §3.* cells (auto-discovered by header) → `feature_df`. Zero drift by construction. |
| §4 | 9–14 | Helpers (`quantile_to_bucket_probs`, `recalibrate_row`), `predict_for_anchor` (season-selective calib + hard floor), PMF bar chart, Kalshi 6-bucket chart |
| §5 | 16 | Fetch Kalshi market, status/quote filter, map contracts → bucket specs, per-contract model P, live Kalshi chart |
| §6 | 18 | Edge analysis on **live contracts only** (mid_edge, ev_yes/no, best side, trade flag at `EDGE_THRESHOLD=0.03`) |
| §7 | 20 | Append per-contract rows to `data/live_log.parquet` (status, is_live, last_price, edge fields) |

### 2.2 Anchor selection (`latest_viable_anchor`)

Picks the latest local date where **all four** hold:
1. wall-clock is past T_HOUR_LOCAL of that date,
2. METAR / TAF / ASOS each have an obs ≥ T_utc,
3. **HRRR parquet covers that date** (else all 17 HRRR features go NaN),
4. (CLI truth optional — used for scoring, not required to predict).

**The binding constraint is ASOS** (~24–48 h IEM lag). With IEM current to "now"
but ASOS lagging ~2 days, the live anchor typically lands ~2 days in the past.
This is the #1 latency limiter — see §5.

### 2.3 Running it

Open `live_predict.ipynb`, set `MODEL = "v3"`, **Run All**. §2 self-refreshes IEM
+ HRRR via subprocesses (uses your kernel's Python → has `metar` + `herbie`).
Override `ANCHOR_DATE = pd.Timestamp("YYYY-MM-DD").normalize()` after the §2
default to force a specific date (e.g., to backtest a settled Kalshi market).

### 2.4 Kalshi integration

- `fetch_kalshi_market(date)` lives in `live.ipynb` cell 14, dynamic-exec'd by §5.
- 6 contracts/day: 1 low-edge (`<=K`), 4 mid 2°F pairs, 1 high-edge (`>=K+7`).
- Subtitle-first parser handles `"X° or below"`, `"X° to Y°"`, `"X° or above"`.
- **Status filter:** settled markets return dummy 0¢/100¢ quotes; §5 flags
  `is_live` from `status ∈ {open, active}`. §6 skips EV math when no live contracts
  (settled days → degenerate edge math).
- Edge: `ev_yes = P_model − yes_ask`, `ev_no = (1−P_model) − no_ask`; trade when
  `best_ev ≥ EDGE_THRESHOLD` (0.03 = 3¢, rough fee buffer — tune from log data).

---

## 3. Data sources & latency

| Source | Used for | Access | Latency | In live pipeline |
|---|---|---|---|---|
| METAR | state @ T, hard floor | api.weather.gov (live), IEM (hist) | ~5 min | `refresh_data.py` |
| TAF | peak-window forecast | IEM | ~min | `refresh_data.py` |
| **ASOS 1-min** | overnight min, running max, temp @ T | IEM | **~24–48 h** ⚠️ | `refresh_data.py` |
| CLI | **target** (daily max) | IEM (NWS climate report) | day-after | `refresh_data.py` |
| HRRR | 17 forecast features | Herbie / AWS `noaa-hrrr-bdp-pds` | 12Z run ~13:30 UTC | `backfill_hrrr.py` |
| Kalshi | market prices | api.elections.kalshi.com | real-time | `fetch_kalshi_market` |

**HRRR backfill:** 12Z init only, fxx≈5–10 (peak window, DST-aware), 13 variables
(`t2m, d2m, u10, v10, gust, mslma, tcc, cape, lftx, sdswrf, sde, sdwe, blh`).
2,337 dates, 2020-01-01 → present, ~5 rows/day. ProcessPoolExecutor (eccodes not
thread-safe), per-fxx tempdir cache isolation, resumable. Full build ≈3.5–6 h at
4 workers; incremental catch-up (live §2) ≈30 s.

---

## 4. Known issues & gotchas

1. **ASOS lag is the live bottleneck.** Live anchor lands ~2 days back. The
   single highest-leverage fix for real-time is replacing IEM ASOS — see §5.
2. **Notebooks can't run under nbconvert** (kernel needs `metar`). Interactive only.
3. **HRRR −2 °F bias misfires on regime changes** (structural; LightGBM at this
   data volume can't learn context-switching). Documented, accepted.
4. **`backfill_hrrr.py` is not interrupt-safe.** A laptop shutdown mid-run cost
   ~6 h of repeated DNS-failure retries. Checkpoints every 100 dates, so it
   resumes, but consider a clean-shutdown handler for future long runs.
5. **`metar`/`herbie` env coupling.** Live §2 subprocesses inherit the kernel's
   Python. If your interactive kernel ever lacks these, §2 fails.
6. **`data/hrrr_12z_KMDW_legacy.parquet`** (9-var pre-path-5 snapshot) can be
   deleted now that the 13-var build is validated.

---

## 5. Real-time / low-latency API options (research)

**Goal:** shrink the live anchor from ~2 days back to **same-day** (or intra-day),
enabling prediction for *today's* 1 PM anchor before the Kalshi market settles.

### 5.1 Where the latency actually is

| Input | Current latency | Needed for same-day | Gap |
|---|---|---|---|
| HRRR | 12Z run ~13:30 UTC (8:30 AM CDT) | ✅ available before 1 PM | none |
| METAR @ T | api.weather.gov ~5 min | ✅ | none |
| TAF | IEM ~min (or api.weather.gov) | ✅ | none |
| **ASOS 1-min** | **IEM ~24–48 h** | sub-hour | **THE bottleneck** |
| CLI (target) | day-after | n/a (it's the label) | n/a |

**Conclusion:** the *only* blocker to same-day live prediction is the ASOS 1-min
feed. Everything else is already low-latency. Three ASOS features
(`asos_min_temp_f_overnight`, `running_max_F_T`, `asos_temp_f_T`) need a real-time
source; the rest of the 46 features are already same-day capable.

### 5.2 Candidate real-time aggregators

| Provider | Aggregates | Latency | 1-min ASOS? | Pricing | Fit |
|---|---|---|---|---|---|
| **Synoptic Data** (ex-MesoWest) | METAR, ASOS, mesonet, 100k+ stations | **sub-minute** | **Yes (native)** | Free tier (generous) + paid | ★ Best fit — purpose-built for real-time surface obs aggregation |
| **Xweather** (AerisWeather) | Obs + models + forecasts | ~min | Via METAR/obs | Paid, tiered | Good if you want obs+model in one API |
| **Tomorrow.io** | Proprietary blend + obs | ~min | Indirect | Paid (no free real-time) | Forecast-centric; less raw-obs control |
| **Meteomatics** | Models + obs blend | ~min | Indirect | Enterprise | Overkill; strong on model data |
| **Visual Crossing** | Obs + historical | ~15–60 min | Hourly only | Freemium | Too coarse (no 1-min) |
| **OpenWeather / Weatherbit** | Current + forecast | ~min | No (synthesized) | Freemium | Synthesized "current" ≠ station ASOS; not suitable |
| api.weather.gov (current) | NWS obs + grids | ~5 min | METAR only (no 1-min) | Free | Already used for METAR; no 1-min ASOS |

### 5.3 Recommendation

**Adopt Synoptic Data's Mesonet API for real-time ASOS.** Rationale:
- It is *the* canonical real-time aggregator for US surface obs (METAR + ASOS
  1-min + mesonets), the same network IEM archives from — but live, sub-minute.
- KMDW is a first-class station with real-time 1-min temperature.
- Free tier likely covers a single-station polling cadence; paid tiers are modest.
- **Train-serve parity:** keep training on IEM's ASOS archive (identical
  underlying network), serve live from Synoptic. The data definitions match, so
  no feature drift — same caveat-discipline as the HRRR train/serve parity.

**Implementation sketch (when ready):**
1. Add `fetch_live_asos_synoptic(station, hours_back)` to `live.ipynb` returning
   the same schema as `refresh_data.py`'s ASOS fetcher (timestamp, temp_f, …).
2. In live `§2`, prefer Synoptic for the recent window, fall back to IEM for the
   historical tail; concat + dedupe.
3. Update `latest_viable_anchor` — ASOS would no longer be the binding
   constraint, so the anchor advances to *today* once past 1 PM.
4. Validate train-serve parity: compare Synoptic-sourced features for a recent
   date against the IEM-sourced equivalents (should match within rounding).

### 5.4 Real-time market data (Kalshi)

Kalshi offers a **WebSocket feed** (`wss://…/trade-api/ws/v2`) for live orderbook
+ ticker updates, vs the REST polling `fetch_kalshi_market` uses now. For paper
trading, REST polling every ~30–60 s is fine. For real-money execution, move to
the WebSocket feed to react to mid-price moves without polling lag. Auth requires
a Kalshi API key (paid tier).

### 5.5 Latency budget for same-day live (post-Synoptic)

| Step | Time |
|---|---|
| HRRR 12Z available | ~08:30 AM CDT |
| Synoptic ASOS/METAR for "now" | sub-minute |
| `live_predict` run (load artifacts + features + score) | ~30 s |
| Kalshi market open until | ~midnight ET (settlement) |

→ Could predict & trade any time from ~9 AM CDT onward, with features fully
current to the 1 PM anchor. This is the unlock that makes the v3 model
operationally tradeable rather than retrospective.

---

## 6. Outstanding work

| Priority | Task | Notes |
|---|---|---|
| **P2** | Backfill **00Z HRRR** → wire HRRR into model_v4 | Add `--init` flag to `backfill_hrrr.py`; enables midnight-anchor HRRR. ~3.5–6 h backfill. |
| **P3** | Real-time ASOS via Synoptic | §5.3 — the same-day unlock. Highest operational leverage. |
| **P4** | Paper-trading loop | `live_log.parquet` schema is ready. Schedule live_predict at each anchor; accumulate 30 days; compare model edge vs realized settlement. |
| **P5** | Real-money execution | Kalshi WebSocket (§5.4), position sizing, fee model, risk limits. |
| cleanup | Delete `hrrr_12z_KMDW_legacy.parquet` | Superseded by 13-var build. |

---

## 7. File inventory

| File | Role |
|---|---|
| `model_v3.ipynb` | ★ Best model — 1 PM anchor, 46 features, CRPS 1.204 °F |
| `model_v4.ipynb` | Midnight-anchor variant (no HRRR yet) |
| `live_predict.ipynb` | Live prediction pipeline (§1–§7) |
| `live.ipynb` | Live fetchers (METAR/TAF/ASOS/HRRR/CLI/Kalshi) + schema parity |
| `backfill_hrrr.py` | HRRR 12Z backfill, 13 vars, resumable |
| `refresh_data.py` | IEM incremental refresh (METAR/TAF/ASOS/CLI) |
| `herbie.ipynb` | HRRR API exploration (reference) |
| `data/*.parquet` | metar, taf, asos, cli, hrrr_12z, live_log |
| `data/model_v3_artifacts/` | Trained models, calibrators, constants, OOF diagnostics |
| `archive/` | Legacy notebooks/scripts (v1/v2, probes) |

### Constants (single source of truth)
```
STATION = "KMDW"             LOCAL_TZ = "America/Chicago"
T_HOUR_LOCAL = 13 (v3) / 0 (v4)   INIT_HOUR_UTC = 12
PEAK_START_HOUR = 12         PEAK_END_HOUR = 16
HRRR_GRID_IY = 665           HRRR_GRID_IX = 1169
SEEDS = [42, 43, 44]         QUANTILES = 0.05..0.95 step 0.05 (19)
INTEGER_F_GRID = arange(-30, 131)   USE_CALIB_FOR_SEASONS = {"JJA"}
```
