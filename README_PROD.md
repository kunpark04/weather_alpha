# Weather Alpha — Production Engine

Terminal-resident, always-on **multi-market** trading bot for Kalshi daily-high markets
(seed: `KXHIGHCHI` at KMDW). It trades a configurable `markets:` list of US cities under one
shared Book, firing each at its own local 1 PM. Ships with the v3 ML model from
`notebooks/model_v3.ipynb` (CRPS 1.204 °F, 46 features) — used only when `model.enabled`; the
current production strategy runs **model-free**.

> **Status:** PAPER, validated end-to-end (the multi-market paper bot runs all configured
> cities clean against the live market). **Production strategy = model-free
> `market_wing + drop_lower_ask`, flat-$ sizing** (config `strategy.name` +
> `model.enabled: false`), dispatched via `engine._dispatch_strategy` → `run_wing_strategy`.
> The v3 model is **not used** (anchor = market; HANDOFF §1.5). Mode is **per-process** (one
> LIVE process, one PAPER process — never mixed in a Book). Hardened through two adversarial
> code-review passes (all findings fixed; 28 tests pass — see [§ Risk & safety](#risk--safety)).
> The authenticated **read** path is validated, but the **first live order/fill is still
> unexercised**. LIVE needs only free Kalshi RSA creds + `mode: live` — no weather feed, no
> same-day anchor. See [§ Going LIVE](#going-live).
>
> **Deployed 2026-06-02:** PAPER (CHI+HOU) + the orderbook logger run 24/7 on a DigitalOcean
> droplet (`systemd --user` + linger); a daily local pull collects the orderbook zips
> (move-mode). The LIVE service is installed but **not started** — Phase 7 paper week in
> progress. Host + pull details: [`deploy/README.md`](deploy/README.md) and HANDOFF §7 `DEPLOY`.

---

## Architecture (multi-market, three engines)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       PRODUCTION ENGINE (main.py)                       │
│  Option B (resident):  main → MarketAnchorScheduler → engine.run_cycle  │
│  Option A (one-shot):  main --headless (per-city, fired by a timer)     │
│  Model path / TUI:     main → scheduler.Scheduler → run_cycle → TUI     │
│                                                                         │
│  run_cycle iterates the `markets:` list under ONE shared Book and       │
│  returns list[CycleResult] (one per city). A per-market failure is      │
│  isolated; settlement is per-station. Mode (live/paper) is per-process. │
└──────────────────────────┬──────────────────────────────────────────────┘
                           │
        ┌──────────────────┼──────────────────┐
        ▼                  ▼                  ▼
┌───────────────┐  ┌────────────────┐  ┌─────────────────┐
│  STRATEGY     │  │  EXECUTION     │  │  SHARED INFRA   │
│  engine       │  │  engine        │  │                 │
│               │  │                │  │  config.py      │
│ market_wing   │  │  paper / live  │  │  log.py         │
│ model-free    │  │  fills, fees,  │  │  model.py       │
│ flat-$ sizing │  │  risk caps,    │  │  pmf.py         │
│ +fee-aware    │  │  exposure cap, │  │  features.py    │
│               │  │  kill switch   │  │  data.py        │
│  strategy.py  │  │  execution.py  │  │  kalshi.py      │
│               │  │  positions.py  │  │  fees.py        │
│               │  │  live_log.py   │  │  report.py      │
└───────────────┘  └────────────────┘  └─────────────────┘
```

`positions.py` holds the shared `Book` (per-city + account drawdown high-water marks and
latched halts); `report.py` is the concise operator stream (ENTER/SKIP/FILLED/SETTLED/HALTED
lines) the resident bot prints. The single-tz `scheduler.Scheduler` still drives the model
path + the TUI; the resident multi-market loop uses `MarketAnchorScheduler` (per-city local
1 PM across any US tz).

**Pure-Python brain per the architecture research.** No C++/Rust in v1 — Kalshi's
latency profile doesn't justify it, and the strategy + execution bottleneck is decision
logic, not microseconds.

---

## Install

```powershell
# from project root
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .[data,dev]      # data: herbie/xarray/cfgrib (HRRR); dev: pytest+ruff
```

Validate the install:
```powershell
python -m weather_alpha --version
python -c "from weather_alpha import config, model, strategy, execution, tui; print('ok')"
```

---

## Configure

Configs (resolution: `--config /abs/path.yaml` > `$WEATHER_ALPHA_CONFIG` > default):

- [`config/weather_alpha.yaml`](config/weather_alpha.yaml) — the default, single-market.
- `config/live.yaml` / `config/paper.yaml` — **resident multi-market** bots (Option B): a
  `markets:` list, one per mode. `live.yaml` = `[CHI]`; `paper.yaml` = `[HOU, CHI]`.
- `config/chicago_live.yaml` / `config/houston_paper.yaml` — per-(mode,tz) **one-shot** (Option A).

`mode: paper` is the default and is safe — no orders are submitted to Kalshi. `mode: live`
requires `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in the environment (or `.env`).

**`markets:`** is a list of `{name, event_pattern, station, local_tz}` — add a city by
appending one line (use the exact IANA tz, incl. no-DST `America/Phoenix`). A legacy
single-market config (top-level `station`/`local_tz` + `execution.market_event_pattern`)
still loads — one market is synthesized. Mode is per-process; never mix live/paper in one Book.

Key knobs:

| Section | Knob | What it does |
|---|---|---|
| `strategy` | `name` | Engine dispatch target. Production = `market_wing` (model-free). |
| `strategy` | `wing.flat_usd` | Flat-$ total stake per trade (production: `2.5`); `0` falls back to Kelly. |
| `strategy` | `wing.fee_aware` | Skip a fire when fees would eat the whole win. |
| `strategy` | `per_contract_max_pct` | Hard per-contract cap as % of bankroll. |
| `strategy` | `total_exposure_max_pct` | Total-exposure cap as % of bankroll — **enforced in `execute()`** (shared across all markets in the process). |
| `risk` | `daily_max_loss_usd` | Halts new entries once realized losses for the day exceed this. |
| `risk` | `per_city_drawdown_pct` | Latch a per-city halt at this drawdown of the running account (default 0.25). |
| `risk` | `account_drawdown_pct` | Latch a whole-account halt at this drawdown (default 0.50). |
| `execution` | `fill_model` | PAPER fill price: `ask` (conservative), `mid`, or `bid`. |
| `execution` | `intraday_refresh_minutes` | How often the post-anchor intraday cycle runs (model path). |

---

## Run

### TUI (production / always-on)

```powershell
python -m weather_alpha
```

Panels:
- **Status bar** — mode, anchor date, bankroll, next-event countdown, last action.
- **Prediction** — top-10 °F buckets of the calibrated PMF for today's anchor.
- **Kalshi market** — strategy targets for the 6 contracts (side, size, prices).
- **Positions** — open positions across all anchors (ticker, side, qty, avg cost).
- **Log** — tail of the in-memory log (DEBUG/INFO/WARNING/ERROR).

Key bindings:

| Key | Action |
|---|---|
| `q` | Quit (clean shutdown — persists Book). |
| `r` | Force a data refresh now. |
| `a` | Force-run today's anchor cycle now. |
| `k` | Write the kill-switch file — refuses any future trade until removed. |

### Headless (cron / one-shot / debug)

```powershell
# One cycle for every market in the config (the latest viable anchor), then exit
python -m weather_alpha --headless

# One cycle, force a specific anchor date
python -m weather_alpha --headless --anchor 2026-05-22

# Skip the slow IEM + HRRR refresh subprocesses (model path only; use existing parquets)
python -m weather_alpha --headless --no-refresh

# RESIDENT multi-market loop (Option B): fires each city at its own local 1 PM, no TUI
python -m weather_alpha --headless --loop --config config/paper.yaml
```

The resident `--loop` is the production shape for "one bot, all cities" — it sleeps until the
next city's anchor (capped at 5 min) via `MarketAnchorScheduler` and prints the `report.py`
ENTER/FILLED/SETTLED lines. See [§ Deploy](#deploy) for the two systemd shapes.

---

## What a cycle does

> **Model-free mode (current production):** steps 1–4 are skipped — no refresh, no feature
> build, no model prediction. The anchor is today's event date, a uniform placeholder PMF is
> used, and step 6 dispatches `market_wing` via `_dispatch_strategy`. The full pipeline below
> runs only when `model.enabled: true` (a model-anchored strategy such as `wing`).

1. **Refresh data.** Subprocess to `refresh_data.py` (METAR/TAF/ASOS/CLI) + `backfill_hrrr.py`
   (12Z HRRR for the last 4 local days). Idempotent; safe to interrupt.
2. **Pick anchor.** `latest_viable_anchor` — latest local date where wall-clock is past 1pm
   *and* METAR/TAF/ASOS each have an obs ≥ T_utc *and* HRRR covers the date. Currently
   gated by ASOS lag (~24–48 h), so the anchor lands ~2 days back. The Synoptic upgrade
   in HANDOFF §5.3 closes this gap.
3. **Build features.** Dynamic-exec `notebooks/model_v3.ipynb` §3.* cells into a controlled
   namespace seeded with the loaded parquets. Same recipe as training — zero feature drift.
4. **Predict.** Score the 3-seed × 19-quantile ensemble, rearrange (Chernozhukov), apply
   season-selective isotonic recalibration (JJA only), apply the v3 hard floor.
5. **Fetch Kalshi.** `KalshiClient.fetch_event(anchor_date)` returns the 6 KXHIGHCHI contracts
   with current quotes + status.
6. **Strategize.** `run_strategy`:
   - Per-contract net-of-fee EV → pick higher side (YES vs NO) per bucket.
   - Per-contract Kelly fraction = `(p_win − cost) / (1 − cost) · b` formula.
   - Re-weight by `KL(P_model || P_market)` contribution per bucket.
   - Apply fractional Kelly + regime throttle.
   - Enforce per-contract and total-exposure caps.
7. **Execute.** PAPER simulates fills at the ask (or mid/bid per config); LIVE submits
   limit orders. Either path: appends to `data/live_log.parquet`, updates the in-memory
   Book, persists `data/positions.json`.
8. **Reconcile settlements.** For every open position whose anchor_date has CLI truth in
   the bundle, resolve to `realized_pnl_cents`.

---

## Risk & safety

Three independent gates, all enforced in `execute()` / the cycle, none of which change sizing
(production sizing is flat **$2.50/trade**, equal-payout):

- **Kill switch** — re-checked **before every order** (stops a multi-leg wing partway); blocks
  **new orders only** — a cycle still settles prior-day positions and re-syncs bankroll. Per
  config (each config has its own `kill_switch` path).
- **Drawdown halts (latched)** — the shared `Book` halts a **city at 25%** drawdown of the
  running account and the **whole account at 50%**, measured from the cumulative-realized peak
  and held until manual reset. A halted city still settles. `scripts/halt.py --status` /
  `--reset [STATION]`.
- **Exposure + daily-loss caps** — `total_exposure_max_pct` (Σ open exposure + the new leg,
  shared across all markets) and `risk.daily_max_loss_usd` both refuse new entries when tripped.

> **Hardening (two adversarial review passes).** Pre-live (2026-05-30): 4 CRITICAL live-path
> bugs fixed — fill confirmation (no phantom positions), deterministic order ids (no duplicate
> orders on retry), intraday risk breakers — plus the WARN-tier robustness gaps (LIVE bankroll
> verified from `get_balance()` + re-synced, scheduler state persisted, event-selection guard,
> per-leg order errors). Multi-market (2026-06-01): a further **2 CRITICAL + 7 WARN + 8 INFO +
> 9 follow-up** findings, all fixed + verified — kill/halt reach the live bot from a credless
> shell, exposure cap enforced, LIVE sizing/cancel use exchange truth, paper bankroll no longer
> double-counts fees, one bad market can't crash the others or trade off-anchor. **28 tests
> pass.** See [`tasks/engine_code_review_2026-06-01.md`](tasks/engine_code_review_2026-06-01.md)
> (Resolution section) and the earlier [`tasks/review_engine_logic.md`](tasks/review_engine_logic.md).
> A stats review ([`tasks/review_backtest_stats.md`](tasks/review_backtest_stats.md)) rates the
> edge real but thin/front-loaded → keep this a **$2.50 toy forward-test; do not scale capital**.

---

## Deploy

Two interchangeable systemd shapes (full runbook: [`deploy/README.md`](deploy/README.md)):

- **Option A — per-(mode,tz) one-shot timers.** `config/{chicago_live,houston_paper}.yaml` +
  `deploy/weather-alpha-{chicago,houston}.{service,timer}` (fire 13:00 local, `AccuracySec=1s`).
  Simplest for a single timezone.
- **Option B — one resident `--loop` bot per mode** across all US tz. `config/{live,paper}.yaml`
  + `deploy/weather-alpha-{live,paper}.service`. The "one bot for all live cities" shape.

A LIVE process **plus** a PAPER process for the *same* city is fine (paper places no real
orders — a useful shadow-test); **never** run two LIVE shapes for one city (they'd double-trade).
Operator tools `scripts/kill.py` and `scripts/halt.py` now **require `--config <the running
bot's config>`** and load with the cred check off, so they work from a credless shell.

---

## Going LIVE

PAPER is the default. The production strategy is **model-free**, so going live is short
(no weather feed, no 30-day model-validation gate, no paid API tier) — but the first real
order is genuinely untested:

- [ ] Create a **free** Kalshi API key — RSA-2048 key pair, upload the public key in Kalshi
      settings, set `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` (read-**write** key for the bot).
- [ ] Confirm the read path: `python scripts/check_kalshi_auth.py` PASSES (it already does with
      both RO and RW keys; balance parses). LIVE adopts the real balance via `get_balance()` at
      startup + re-syncs after settlement — config `bankroll_usd` is only a hint. Do not start large.
- [~] **Phase 7 — running**: the PAPER bot (`config/paper.yaml`, both cities) is live 24/7 on
      the DO droplet since 2026-06-02 (HANDOFF §7 `DEPLOY`) — watch a few 1 PM CT cycles before
      flipping any city LIVE. The loop/settlement/risk/reporter are validated in paper; the
      **LIVE order-placement + real-fill path is still unexercised** (0 open positions).
- [ ] Set `risk.daily_max_loss_usd` + the drawdown pcts to numbers you can lose without flinching.
- [ ] Dry-run a single LIVE city by hand and read the log — confirm the orders that *would* be
      placed match intuition. (Model-free needs no `--anchor` or weather; anchor = today's event.)
- [x] Deploy on an always-on host — ✅ DONE: PAPER + logger run 24/7 on a DO droplet (HANDOFF §7 `DEPLOY`). For LIVE, upload the read-write key to the VM, then `systemctl --user enable --now weather-alpha-live.service`.

> Retired gates: the old "30 paper days" and "adopt Synoptic for same-day data" requirements
> no longer apply — `market_wing` is model-free (HANDOFF §1.5), and at $25 this is deliberate
> low-stakes live-learning.

Flip a single city LIVE by pointing its process at a `mode: live` config (e.g. `config/live.yaml`
or `config/chicago_live.yaml`). In the TUI the status bar shows `[LIVE]` in red.

**Kill switch — halt at any time, from any terminal:**
```powershell
python scripts/kill.py --config config/live.yaml            # ARM  — halts before the next order
python scripts/kill.py --status --config config/live.yaml   # check
python scripts/kill.py --disarm --config config/live.yaml   # resume
```
Re-checked **before every order** (stops a multi-leg wing partway); blocks **new orders only** —
a cycle still settles prior-day positions and re-syncs bankroll. It does **not** cancel orders
already resting on the exchange — use the Kalshi UI. (The TUI's `k` key writes the same
`KILL_SWITCH` file.) Drawdown halts use the sibling `scripts/halt.py --status / --reset` (also
`--config`).

---

## Known limitations (v1)

| # | Limitation | Mitigation |
|---|---|---|
| 1 | Anchor lag (IEM ASOS, ~2 days) — **only affects `model.enabled` mode**; the production model-free path uses today's event date. | Obsolete for `market_wing`; adopt Synoptic only if running a model-anchored strategy. |
| 2 | LIVE order/fill path is **unexercised** (0 open positions to date) — and the Book isn't yet reconciled against `get_positions()` at *cycle start*, so an out-of-band or mid-exception fill stays unbooked. | Phase 7 paper week first; add a cycle-start `get_positions()` reconcile (HANDOFF `RECON`) before scaling capital. |
| 3 | The **TUI still drives trading** (old single-tz scheduler + `run_cycle`) rather than being a read-only monitor — don't run it as a passive viewer yet. | Convert to read-only `/portfolio` polling (the one deferred review item; HANDOFF `TUI`). |
| 4 | LIVE books the observed avg fill price (C1), not the limit; the booked *fee* is still estimated rather than read from `/portfolio/fills`. | Reconcile fees from `/portfolio/fills` (I4) — minor at $25. |
| 5 | Features come from dynamic-exec of `notebooks/model_v3.ipynb` (fragile if the notebook moves) — model path only. | Port §3.* cells into `weather_alpha/features_v3.py` and validate parity. |
| 6 | Kalshi WebSocket is not wired (REST polling only). | Add `kalshi.ws` and switch real-time market state to WS feed. |

These are tracked in HANDOFF.md §7 (open items) and §6 (research-side outstanding work).

---

## File reference

| Module | Role |
|---|---|
| `weather_alpha/config.py` | Dataclass config; `markets:` list (+ single-market back-compat), `MarketCfg`/`RiskCfg` drawdown pcts, `config_path`, `load_config(require_live_creds=)`. |
| `weather_alpha/log.py` | Rotating file log + in-memory ring buffer for the TUI. |
| `weather_alpha/model.py` | Loads `data/model_v3_artifacts/`, `predict_for_anchor` (model path only). |
| `weather_alpha/pmf.py` | Quantile→PMF, bucket parsers, KL, market-implied PMF. |
| `weather_alpha/features.py` | Dynamic-exec of `model_v3.ipynb` §3.* into a controlled namespace. |
| `weather_alpha/data.py` | `refresh_iem`, `refresh_hrrr`, `load_bundle`, `latest_viable_anchor`. |
| `weather_alpha/kalshi.py` | Async REST client (public + RSA-PSS-signed); parser reads `subtitle`/`yes_sub_title` + `between`/`less`/`greater`; locale-safe `event_date_code`. |
| `weather_alpha/fees.py` | Kalshi 7%·N·P·(1−P) fee formula. |
| `weather_alpha/strategy.py` | 8 strategy fns; `run_wing_strategy` (production: market_wing, flat-$, fee-aware). |
| `weather_alpha/positions.py` | `Position`(+`station`) + shared `Book` — per-city/account drawdown HWM + latched halts, JSON snapshot. |
| `weather_alpha/execution.py` | Paper/live fills; kill switch, exposure + daily-loss caps, per-anchor cap; settlement reconciliation. |
| `weather_alpha/report.py` | Concise operator terminal stream (ENTER/SKIP/FILLED/SETTLED/HALTED). |
| `weather_alpha/live_log.py` | Append rows to `data/live_log.parquet`. |
| `weather_alpha/scheduler.py` | `Scheduler` (single-tz: refresh/anchor/intraday) + `MarketAnchorScheduler` (per-tz, resident bot). |
| `weather_alpha/engine.py` | `run_cycle` — iterates `markets` under one Book → `list[CycleResult]`; per-station settlement; drawdown latching. |
| `weather_alpha/tui.py` | Textual app — five panels, key bindings, scheduler dispatch (⚠️ still an active trading driver). |
| `weather_alpha/main.py` | Entry point — TUI by default, `--headless [--loop]` for one-shot / resident multi-market loop. |

Reference notebooks (input to production code, not run by it):
- `notebooks/model_v3.ipynb` — ★ production model trainer.
- `notebooks/live_predict.ipynb` — original Jupyter pipeline (replaced by this package).
- `notebooks/live.ipynb` — fetchers + Kalshi market client (ported to `weather_alpha/kalshi.py`).
- `notebooks/herbie.ipynb` — HRRR exploration / reference.

Existing scripts (still used as subprocesses):
- `refresh_data.py` — IEM METAR/TAF/ASOS/CLI refresh.
- `backfill_hrrr.py` — 12Z HRRR backfill via Herbie/AWS.
