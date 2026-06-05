# CLAUDE.md — Weather Alpha project guide

Architectural and behavioral guide for Claude working in this repo. Detailed
results, retrain steps, and reproducible-command snippets live in [`HANDOFF.md`](HANDOFF.md).

---

## 1. What this project is

A probabilistic ML forecaster + **multi-market** production trading engine for Kalshi
daily maximum-temperature markets. The engine trades a **configurable list of US cities**
(each `{name, event_pattern, station, local_tz}`), firing each at *its own* local 1 PM;
Chicago **`KXHIGHCHI`** at **KMDW** (Chicago Midway) is the seed market. The production
strategy is the **model-free** `market_wing + drop_lower_ask` — it anchors on the market,
not the model, so it maps each event's 6 Kalshi buckets to a wing without any weather feed.
The v3 model (calibrated PMF over integer °F at 1 PM local, mapped onto the buckets) is
retained for the model-enabled path but is **not** used by the production strategy.

**Mode is per-process** (one LIVE process, one PAPER process — never mixed in a Book). Live
is wired + the authenticated read path is validated, but the first live order/fill is still
unexercised until it fires at the next 1 PM CT anchor. **Live since 2026-06-02:** a LIVE bot
(Chicago) + a PAPER bot + orderbook logger run 24/7 on a DigitalOcean droplet
(`systemd --user`), with a daily local pull of the orderbook zips; the LIVE bot is armed and
adopted the real $23.56 balance. **As of 2026-06-05** the PAPER bot shadows **all 20 cities** and
the logger logs **all 20 series** (both authenticate the RW key *read-only*; paper still places no
real orders). A deep-history study (`/historical/*`, 3.4 yr × 20 cities) found **only Chicago has a
durable edge** and **maker execution doesn't help** (adverse selection). See
[`HANDOFF.md`](HANDOFF.md) §1.5, §1.6, §7 (`DEPLOY`/`L2`).

---

## 2. Architecture

```
notebooks/model_v3.ipynb            <-- model R&D, retraining, calibration
        |
        v  saved artifacts (joblib + npy + parquet) -- used ONLY when model.enabled
data/model_v3_artifacts/
        |
        v  loaded by
weather_alpha/                     <-- production package (always-on bot)
   model.py        Prediction dataclass + artifact loader
   pmf.py          PMF utilities, bucket parsing, rearrangement
   kalshi.py      KalshiContract + parser (subtitle OR yes_sub_title; between/less/greater);
                  event_date_code() locale-safe ticker date
   live_fetchers.py     async METAR/TAF/ASOS/HRRR/CLI/Kalshi
   strategy.py    8 strategy fns; run_wing_strategy is production (flat-$, fee-aware)
   calibration.py LOO alpha (currently 0 -- market-only; unused on the model-free path)
   fees.py        Kalshi fee = ceil(7% * N * P * (1-P))
   execution.py   paper/live order routing; kill switch + exposure/daily-loss caps
   positions.py   Position(+station) + Book (per-city & account drawdown HWM, latched halts)
   engine.py      orchestrator: run_cycle iterates markets under ONE shared Book ->
                  list[CycleResult]; per-market isolation; per-station settlement
   report.py      concise operator stream (ENTER/SKIP/FILLED/SETTLED/HALTED lines)
   scheduler.py   Scheduler (single-tz, model path) + MarketAnchorScheduler (per-tz, resident bot)
   tui.py         Textual TUI
   main.py        entry point: resident --headless --loop multi-market loop (+ single-tz model loop)
        |
        v  driven by
config/weather_alpha.yaml          <-- default knobs (bankroll, kelly, throttle, markets)
config/{live,paper}.yaml           <-- resident multi-market bots (Option B; markets: list)
config/{chicago_live,houston_paper}.yaml  <-- per-(mode,tz) one-shot timers (Option A)
```

Three orthogonal layers: **model** (notebook + artifacts), **strategy/execution**
(`weather_alpha/`), **config** (YAML). Strategies are pure functions of
`(StrategyCfg, Prediction, contracts, feature_row, bankroll)` → `StrategyOutput`.

**Multi-market design.** A config carries a `markets:` list — each a `MarketCfg`
(`{name, event_pattern, station, local_tz}`). `run_cycle` iterates the markets (or a due
subset) under **one shared `Book`/bankroll** and returns `list[CycleResult]` (one per
market); a per-market failure is isolated so one city can't crash the others. Settlement is
**per-station** (`settle_market_if_due` pulls each city's CLI high on demand). A legacy
single-market config (top-level `station`/`local_tz` + `execution.market_event_pattern`)
still loads — one market is synthesized — so full back-compat holds.

**Risk model.** `Book` tracks per-city + whole-account drawdown high-water marks and
**latched halts**: a city stops at **25%** drawdown of the running account, the whole
account at **50%**, both measured from the peak and latched until manual reset
(`scripts/halt.py --reset … --config <cfg>`). `total_exposure_max_pct` is enforced in
`execute()`. Sizing is unchanged — flat **$2.50/trade**, equal-payout; the drawdown stops
are an orthogonal gate, not a sizing change.

**Two deploy shapes (pick one per city).** *Option A* — per-(mode,tz) one-shot `systemd
--user` timers (`config/{chicago_live,houston_paper}.yaml` + `deploy/weather-alpha-{chicago,houston}.{service,timer}`,
fire 13:00 local). *Option B* — one **resident** `--headless --loop` bot per mode across all
US tz (`config/{live,paper}.yaml` + `deploy/weather-alpha-{live,paper}.service`), where
`MarketAnchorScheduler` fires each city at its own local 1 PM (DST-correct, 60-min
post-anchor window, no off-anchor catch-up on a late restart). `live.yaml` markets=[CHI];
`paper.yaml` markets=[HOU, CHI] (paper shadows live Chicago). A LIVE + a PAPER process for
the *same* city is fine (paper places no real orders); never run two LIVE shapes for one city.

**Three live runtime systems — one writer, two readers:**
1. **Bot** (resident `python -m weather_alpha --headless --loop`, or per-city one-shots fired by a
   `deploy/weather-alpha-*.timer`) — the only writer; owns the trade lifecycle (decide → place →
   settle) + the Book; **read-write** key.
2. **Orderbook logger** (`scripts/orderbook_logger.py`) — keyless, public market-depth collection only.
3. **Monitor (TUI)** — *intended* as a read-only view of positions + account P/L from Kalshi
   `/portfolio` (**read-only** key). ⚠️ Today the TUI **also drives trading** (duplicate single-tz
   scheduler + `run_cycle`); turning it into a pure read-only monitor is the one deferred
   review item — see [`HANDOFF.md`](HANDOFF.md) §7 "TUI".

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
11. **LIVE bankroll is the real Kalshi balance, not config.** `engine.verify_bankroll`
    adopts `get_balance()` at activation and re-syncs after each settlement;
    `strategy.bankroll_usd` is only a paper default / hint. Don't reintroduce
    config-based sizing on the live path.
12. **`model.enabled=false` gates DATA acquisition, not just decisions.** A model-free
    strategy fetches no weather and loads no bundle: `refresh_data` no-ops, `run_cycle`
    skips `load_bundle`, and settlement pulls only the CLI high on demand via
    `engine.settle_if_due` (prior-day trigger). Don't re-couple the model-free path to
    METAR/TAF/ASOS/HRRR, `herbie`, `load_bundle`, or model artifacts.
13. **Maker execution is adversely selected here — posting at the bid is NOT free spread.** A
    resting bid fills only when the market sells into it, which happens disproportionately when the
    bucket is turning into a loser (winning legs fill *less* than losing legs in 14/20 cities;
    Chicago 32% vs 42%). The production path is a **limit-at-ask taker** on purpose; don't "save the
    spread" with a maker leg without re-checking outcome-split fill rates. Tested 2026-06-05 on the
    deep tape via `taker_side` (`scripts/maker_fill_harness.py`; [`HANDOFF.md`](HANDOFF.md) §1.6;
    `tasks/lessons.md` L15).

---

## 5. Conventions

- **Tables / structured output:** present tabular or comparison data as a standard
  **GitHub-flavored markdown table** (pipe rows + a `|---|` header-separator line). Keep it
  **compact so it renders as a clean grid** — few columns, concise cells; don't pack
  paragraph-length text into a cell (push detail to prose/footnotes below). Inline `code`
  for identifiers/paths/values, **bold**/*italics* sparingly, emoji where they aid scanning.
  No ASCII-art boxes; never replace a table with a bullet list for tabular data.
- **Working directory** is the project root (`weather-alpha/`). All relative paths
  in scripts assume this.
- **Times** are tz-aware. Local = `America/Chicago`. Anchor = 1 PM local
  = `13:00 CT` = `18:00 UTC` (CST) / `19:00 UTC` (CDT).
- **Units** are °F throughout (model + features + truth). Internal HRRR data was
  K originally; conversion happens once in `model_v3.ipynb` §3.
- **Bucket spec strings:** `"72-73"` (inclusive range), `"<=71"` (lower tail),
  `">=80"` (upper tail). Parsed by `weather_alpha/pmf.py:parse_kalshi_subtitle`.
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
- **Modify `weather_alpha/strategy.py` only with intent.** Function signatures
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
| [`README_PROD.md`](README_PROD.md) | Production engine (`weather_alpha/`) architecture, paper-mode setup, going-live checklist |
| [`Language and Architecture Choices for a Production-Grade Kalshi Trading Bot in the Terminal.md`](Language%20and%20Architecture%20Choices%20for%20a%20Production-Grade%20Kalshi%20Trading%20Bot%20in%20the%20Terminal.md) | Research doc — Python/C++/Rust/Go trade-offs, TUI framework choices, Kalshi SDK landscape |
| `notebooks/model_v3.ipynb` | Production model notebook — features, CV, calibration, §10.3 Kalshi-resolution diagnostic |
| `notebooks/model_v4.ipynb` | Parallel R&D variant — midnight anchor (NOT production) |
| `notebooks/live_predict.ipynb` | Live prediction pipeline (loads v3 artifacts) |
| `config/weather_alpha.yaml` | Default strategy/risk config (single-market); multi-market lives in `config/{live,paper}.yaml`, per-tz one-shots in `config/{chicago_live,houston_paper}.yaml` |
| [`deploy/README.md`](deploy/README.md) | Always-on deployment guide — orderbook-logger + the two bot shapes (Option A per-city timers / Option B resident multi-market), operator tools (`scripts/kill.py`, `scripts/halt.py` — both need `--config`) |
| [`tasks/multimarket_refactor_plan.md`](tasks/multimarket_refactor_plan.md) | Multi-market refactor design + locked decisions (markets list, shared Book, per-tz scheduler, drawdown halts) |
| [`tasks/engine_code_review_2026-06-01.md`](tasks/engine_code_review_2026-06-01.md) | Adversarial engine review (2 CRITICAL · 7 WARN · 8 INFO + follow-up) with a Resolution section — all fixed; TUI read-only conversion deferred |
| [`tasks/lessons.md`](tasks/lessons.md) | Self-improvement log — recurring-mistake patterns + prevention rules |

Excluded from the index (auto-generated or vendored, no managerial role):
`__pycache__/`, `archive/` (legacy notebooks/scripts), `logs/`, `data/*.parquet`.

**Hygiene rule:** when adding any managerial md file (decision log, runbook, lessons),
also add a row to this table in the same commit.
