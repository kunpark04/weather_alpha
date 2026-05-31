# Weather Alpha — Production Engine

Terminal-resident, always-on trading bot for Kalshi `KXHIGHCHI` (KMDW daily-high).
Ships with the v3 ML model from `notebooks/model_v3.ipynb` (CRPS 1.204 °F, 46 features)
— used only when `model.enabled`; the current production strategy runs **model-free**.

> **Status:** PAPER, validated end-to-end. **Production strategy = model-free
> `market_wing + drop_lower_ask`, flat-$ sizing** (config `strategy.name` +
> `model.enabled: false`), dispatched via `engine._dispatch_strategy` →
> `run_wing_strategy`. The v3 model is **not used** (anchor = market; HANDOFF §1.5).
> LIVE needs only free Kalshi RSA creds + `mode: live` — no weather feed, no same-day
> anchor. See [§ Going LIVE](#going-live).

---

## Architecture (three engines)

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       PRODUCTION ENGINE (main.py)                       │
│   main → scheduler.Scheduler → engine.run_cycle → TUI                   │
│                                                                         │
│   Textual TUI is the always-on window. Scheduler ticks once a minute   │
│   and dispatches DATA_REFRESH / ANCHOR / INTRADAY to run_cycle.         │
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
│ +fee-aware    │  │  kill switch   │  │  features.py    │
│               │  │                │  │  data.py        │
│  strategy.py  │  │  execution.py  │  │  kalshi.py      │
│               │  │  positions.py  │  │  fees.py        │
│               │  │  live_log.py   │  │                 │
└───────────────┘  └────────────────┘  └─────────────────┘
```

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

Single source of truth: [`config/weather_alpha.yaml`](config/weather_alpha.yaml).

- `mode: paper` is the default and is safe — no orders are submitted to Kalshi.
- `mode: live` requires `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` in the
  environment (or `.env` — copy from `.env.example`).
- Override the YAML path with `--config /abs/path.yaml` or `$WEATHER_ALPHA_CONFIG`.

Key knobs:

| Section | Knob | What it does |
|---|---|---|
| `strategy` | `kelly_fraction` | Fractional Kelly (default 0.25). |
| `strategy` | `kl_concentration_alpha` | 0 = uniform across positive-edge buckets; 1 = full KL-weighted concentration. |
| `strategy` | `edge_floor_cents` | Minimum net-of-fee EV (cents) before opening a position. |
| `strategy` | `per_contract_max_pct` | Hard per-contract cap as % of bankroll. |
| `strategy` | `total_exposure_max_pct` | Hard total-exposure cap as % of bankroll. |
| `risk` | `daily_max_loss_usd` | Halts new entries once realized losses for the day exceed this. |
| `execution` | `fill_model` | PAPER fill price: `ask` (conservative), `mid`, or `bid`. |
| `execution` | `intraday_refresh_minutes` | How often the post-anchor intraday cycle runs. |

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
# One cycle for the latest viable anchor, then exit
python -m weather_alpha --headless

# One cycle, force a specific anchor date
python -m weather_alpha --headless --anchor 2026-05-22

# Skip the slow IEM + HRRR refresh subprocesses (use existing parquets)
python -m weather_alpha --headless --no-refresh

# Stay-alive scheduler loop without TUI
python -m weather_alpha --headless --loop
```

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

## Going LIVE

PAPER is the default. The production strategy is **model-free**, so going live is short
(no weather feed, no 30-day model-validation gate, no paid API tier):

- [ ] Create a **free** Kalshi API key — generate an RSA-2048 key pair, upload the public
      key in Kalshi settings, save the private key locally, set `KALSHI_KEY_ID` and
      `KALSHI_PRIVATE_KEY_PATH` in `.env`. (Kalshi API access is free; no paid tier.)
- [ ] `strategy.bankroll_usd` is already `25` — but in LIVE the engine verifies the real
      balance via `get_balance()` at startup and re-syncs after each settlement, so the config
      value is only a paper default / hint. Do not start large.
- [ ] Set `risk.daily_max_loss_usd` to a number you can lose without flinching; execution
      refuses new entries once daily realized losses exceed it.
- [ ] Dry-run: `python -m weather_alpha --headless --no-refresh` and read the log — confirm
      the orders that *would* be placed match intuition. (Model-free needs no `--anchor` or
      weather; the anchor is simply today's event date.)
- [ ] Deploy on an always-on host so the 1 PM anchor fires with your laptop off — see
      [`deploy/README.md`](deploy/README.md).

> Retired gates: the old "30 paper days" and "adopt Synoptic for same-day data" requirements
> no longer apply — `market_wing` is model-free (HANDOFF §1.5), and at $25 this is deliberate
> low-stakes live-learning.

When those are green, edit `config/weather_alpha.yaml`:
```yaml
mode: live
```
and `python -m weather_alpha`. The status bar will show `[LIVE]` in red.

**Kill switch — halt at any time, from any terminal:**
```powershell
python scripts/kill.py            # ARM  — bot halts before the next order (even mid-cycle)
python scripts/kill.py --status   # check
python scripts/kill.py --disarm   # resume
```
The engine re-checks the switch **before every order**, not just at cycle start, so arming it
stops a multi-leg wing partway. Arming it blocks **new orders only** — a cycle still settles
prior-day positions and re-syncs bankroll (fixed 2026-05-31). It does not cancel orders already resting on the exchange —
use the Kalshi UI for that. (The TUI's `k` key writes the same `data/KILL_SWITCH` file.)

> **Pre-live order-safety review (2026-05-30):** an adversarial engine review found and fixed
> 4 CRITICAL live-path bugs — fill confirmation (no phantom positions), deterministic order
> ids (no duplicate orders on retry), and intraday risk circuit breakers. A follow-up pass then
> filled the WARN-tier robustness gaps — LIVE bankroll verified from `get_balance()` + re-synced
> after settlement, scheduler state persisted across restart, an event-selection guard, and
> per-leg order-error handling (W1-W5). See
> [`tasks/review_engine_logic.md`](tasks/review_engine_logic.md). A stats review
> ([`tasks/review_backtest_stats.md`](tasks/review_backtest_stats.md)) rates the edge real but
> thin/front-loaded → keep this a **$2.50 toy forward-test; do not scale capital** on it.

---

## Known limitations (v1)

| # | Limitation | Mitigation |
|---|---|---|
| 1 | Anchor lag (IEM ASOS, ~2 days) — **only affects `model.enabled` mode**; the production model-free path uses today's event date. | Obsolete for `market_wing`; adopt Synoptic only if running a model-anchored strategy. |
| 2 | LIVE books only confirmed fills (C1 polls `get_positions`), but doesn't yet reconcile the Book against `get_positions()` at *cycle start* — an out-of-band or mid-exception fill stays unbooked. | Add a cycle-start `get_positions()` reconcile (the C1/W5 tail) before scaling capital. |
| 3 | LIVE books the observed avg fill price (C1), not the limit; the booked *fee* is still estimated from that price rather than read from `/portfolio/fills`. | Reconcile fees from `/portfolio/fills` (I4) — minor at $25. |
| 4 | Features come from dynamic-exec of `notebooks/model_v3.ipynb` (fragile if the notebook moves). | Port §3.* cells into `weather_alpha/features_v3.py` and validate parity. |
| 5 | Kalshi WebSocket is not wired (REST polling only). | Add `kalshi.ws` and switch real-time market state to WS feed. |
| 6 | Regime throttle uses simple thresholds; no learned regime classifier. | Train a regime classifier on `live_log.parquet` once it has ≥ 60 days. |

These are tracked in HANDOFF.md §6 (research-side outstanding work).

---

## File reference

| Module | Role |
|---|---|
| `weather_alpha/config.py` | Dataclass config loaded from `config/weather_alpha.yaml`. |
| `weather_alpha/log.py` | Rotating file log + in-memory ring buffer for the TUI. |
| `weather_alpha/model.py` | Loads `data/model_v3_artifacts/`, `predict_for_anchor`. |
| `weather_alpha/pmf.py` | Quantile→PMF, bucket parsers, KL, market-implied PMF. |
| `weather_alpha/features.py` | Dynamic-exec of `model_v3.ipynb` §3.* into a controlled namespace. |
| `weather_alpha/data.py` | `refresh_iem`, `refresh_hrrr`, `load_bundle`, `latest_viable_anchor`. |
| `weather_alpha/kalshi.py` | Async REST client (public + RSA-PSS-signed endpoints). |
| `weather_alpha/fees.py` | Kalshi 7%·N·P·(1−P) fee formula. |
| `weather_alpha/strategy.py` | Joint Kelly + KL concentration + regime throttle. |
| `weather_alpha/positions.py` | Per-position state + JSON snapshot persistence. |
| `weather_alpha/execution.py` | Paper/live fills, risk gates, settlement reconciliation. |
| `weather_alpha/live_log.py` | Append rows to `data/live_log.parquet`. |
| `weather_alpha/scheduler.py` | Pure-logic scheduler: refresh/anchor/intraday decisions. |
| `weather_alpha/engine.py` | `run_cycle` — one full anchor cycle end-to-end. |
| `weather_alpha/tui.py` | Textual app — five panels, key bindings, scheduler dispatch. |
| `weather_alpha/main.py` | Entry point — TUI by default, `--headless` for one-shot/loop. |

Reference notebooks (input to production code, not run by it):
- `notebooks/model_v3.ipynb` — ★ production model trainer.
- `notebooks/live_predict.ipynb` — original Jupyter pipeline (replaced by this package).
- `notebooks/live.ipynb` — fetchers + Kalshi market client (ported to `weather_alpha/kalshi.py`).
- `notebooks/herbie.ipynb` — HRRR exploration / reference.

Existing scripts (still used as subprocesses):
- `refresh_data.py` — IEM METAR/TAF/ASOS/CLI refresh.
- `backfill_hrrr.py` — 12Z HRRR backfill via Herbie/AWS.
