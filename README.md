# Forecast Alpha

Probabilistic ML model + production trading engine for **Kalshi `KXHIGHCHI`** —
the daily maximum-temperature market at **KMDW** (Chicago Midway).

The model produces a calibrated PMF over integer °F at 1 PM local; the engine
maps that PMF onto the day's 6 Kalshi buckets and decides whether and how to
trade. The whole bot is designed to run in an always-on terminal window.

---

## Current state

**Mode:** paper trading (Kalshi key not yet enabled for live).

> **Production pivoted 2026-05-29 → `market_wing + drop_lower_ask`, flat-$ sizing, model-free.**
> Wired into the engine and validated in paper (no model, no weather, no creds). On the real
> **$25** account (flat $2.50, fee-aware): **24 fires, 96 % WR, +$7.98 (+32 %), −7 % DD**. The
> table below is the *prior* Kelly-sized `wing` result; full rationale in [`HANDOFF.md`](HANDOFF.md) §1.5.

| Item | Value |
|---|---|
| Production strategy | **`market_wing + drop_lower_ask`**, flat-$, model-free *(pivoted 2026-05-29; table below = prior `wing`/Kelly result)* |
| Backtest window | 2026-03-21 → 2026-05-26 (67 Kalshi days) |
| Fires on | 22 of 67 days |
| Win rate | **91 %** (20 / 2 day-level W/L) |
| Net PnL | **+$308** on $1000 starting bankroll |
| Sharpe (annualized) | **+2.85** |
| Max drawdown | -$181 (-18 %) |
| Placebo (`drop_higher_ask`) | -$431 — confirms directional signal |
| Production model | `model_v3` (1 PM anchor, 46 features) |
| Model CRPS | 1.204 °F |
| Top-1 / Top-3 (±1 bucket) | 45.8 % / 87.8 % over 1,966 OOF days |
| Top-3 on agreement days | **100 %** (61-day window, Kalshi resolution) |

See [`HANDOFF.md`](HANDOFF.md) for the full strategy verdict, sizing pipeline,
and open items.

---

## Project structure

```
forecast-alpha/
├── README.md                    you are here — project overview
├── HANDOFF.md                   durable hand-off (full results, open items)
├── CLAUDE.md                    architecture + behavioral guide for Claude
├── README_PROD.md               production engine README (forecast_alpha/)
├── Language and Architecture Choices ... .md   research / language trade-offs
│
├── notebooks/
│   ├── model_v3.ipynb           PRODUCTION model — features, CV, calibration, §10.3 Kalshi-resolution diagnostic
│   ├── model_v4.ipynb           parallel R&D variant (midnight anchor; NOT production)
│   └── live_predict.ipynb       live prediction pipeline (loads v3 artifacts)
│
├── forecast_alpha/              production package (always-on bot)
│   ├── main.py / engine.py / scheduler.py / tui.py    orchestration + TUI
│   ├── strategy.py              8 strategy fns; run_wing_strategy is production
│   ├── execution.py             paper/live order routing
│   ├── live_fetchers.py         async METAR/TAF/ASOS/HRRR/CLI/Kalshi
│   ├── model.py / pmf.py / kalshi.py / fees.py / calibration.py
│   └── config.py                YAML loader
│
├── scripts/                     backtests, diagnostics, one-off analyses
│   ├── backtest_strategy.py     ★ main backtest harness — walk-forward, intraday exits
│   ├── all_variants_retest.py   12-variant strategy comparison
│   ├── market_wing_test.py      market_wing variants (anchor on market modal)
│   ├── drop_lower_ask_oos.py    3-part OOS validation (early / late / placebo)
│   ├── sizing_breakdown.py      per-day stake decomposition
│   ├── verify_bucket_accuracy.py    §10.2 / §10.3 bucket-accuracy verification
│   ├── full_67day_backtest.py   filtered vs unfiltered on full Kalshi window
│   ├── model_signal_stability_2021_2026.py    5-yr model-side stability check
│   ├── kalshi_history.py        pulls settled KXHIGHCHI events + trades
│   ├── settle.py                daily settlement reconciliation
│   └── backfill_hrrr.py / refresh_data.py    historical data backfill
│
├── config/
│   └── forecast_alpha.yaml      bankroll, kelly_fraction, throttle, etc.
│
├── data/
│   ├── kalshi_history.parquet   67 Kalshi events + per-ticker trades (Mar-May 2026)
│   ├── cli_KMDW.parquet         NWS CLI daily-max truth (2015-present)
│   ├── metar / taf / asos / hrrr parquets    feature-table sources
│   ├── live_log.parquet         per-contract prediction log (paper-mode)
│   └── model_v3_artifacts/      trained ensemble + OOF + calibrators
│
├── logs/                        runtime logs from the live bot
├── tasks/                       work-in-progress, lessons, agent artifacts
└── archive/                     legacy notebooks / superseded scripts
```

---

## Reproducing the backtest

The closest runnable backtest for the production `market_wing` (Kelly-sized — the
harness does not yet expose flat-$ flags):

```powershell
python scripts/backtest_strategy.py `
    --strategy market_wing `
    --wing-drop-lower-ask `
    --wing-assumed-win-prob 0.92 `
    --wing-max-ask-sum 0.90 `
    --out data/market_wing_backtest.parquet
```

The **prior** production result (`wing` + Kelly): swap `--strategy wing
--wing-assumed-win-prob 0.99 --wing-max-ask-sum 1.00` → 22 fires, +$308, 20/2 W/L.

> ⚠️ The headline **+$7.98 / +32 % on $25** figure uses **flat-$ ($2.50) + fee-aware**
> sizing, which lives only in `run_wing_strategy` / the engine config path
> (`model.enabled: false`, `strategy.name: market_wing`, `wing.flat_usd: 2.5`) — **not**
> in `backtest_strategy.py`. It was produced by an inline flat-$ analysis this session.
> **TODO:** expose `--wing-flat-usd` / `--wing-fee-aware` in the backtest harness so the
> production figure is reproducible from one command. Other scripts: [`HANDOFF.md`](HANDOFF.md) §6.

---

## Retraining the model

Open `notebooks/model_v3.ipynb` → **Run All** (~21 min). Final cell saves
artifacts to `data/model_v3_artifacts/`. Notebooks need the `metar` and `herbie`
packages installed in the kernel — interactive only (not nbconvert-safe).

See [`HANDOFF.md`](HANDOFF.md) §4 for the feature inventory and key design
decisions.

---

## Documentation map

| Doc | When to read |
|---|---|
| [`HANDOFF.md`](HANDOFF.md) | Picking up the project after a break, or onboarding |
| [`CLAUDE.md`](CLAUDE.md) | Working in this repo as Claude (or as a human who wants the same context) |
| [`README_PROD.md`](README_PROD.md) | Running or extending the always-on production engine (`forecast_alpha/`) |
| [`Language and Architecture Choices ...`](Language%20and%20Architecture%20Choices%20for%20a%20Production-Grade%20Kalshi%20Trading%20Bot%20in%20the%20Terminal.md) | Understanding why Python + Textual + LightGBM (vs C++/Rust/Go alternatives) |
| `notebooks/model_v3.ipynb` | Retraining the model or inspecting feature contributions |

---

## Open priorities

From [`HANDOFF.md`](HANDOFF.md) §7:

1. ✅ **Strategy wired** — model-free `market_wing` + flat-$ dispatched from config (replaced `joint_kelly`)
2. **Deploy the orderbook logger always-on** — `scripts/orderbook_logger.py` on a VM/Pi (`deploy/README.md`); keyless, survives reboots
3. **Go live** — add Kalshi RSA creds (`KALSHI_KEY_ID` / `KALSHI_PRIVATE_KEY_PATH`) + `mode: live`, run the engine at the 1 PM anchor ($25)
4. Expose flat-$ in `backtest_strategy.py` (`--wing-flat-usd` / `--wing-fee-aware`) so the production figure reproduces from one command
5. Liquidity verification — the logger's depth ladders now answer "do we fill at `mid + 1¢`?"; fill reconciliation deferred (low-$ at $25)
