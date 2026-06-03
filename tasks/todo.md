# TODO — ship `market_wing + drop_lower_ask` live on a $25 Kalshi account

Strategy chosen: **market_wing + drop_lower_ask**, **flat-$ sizing** (see memory
`project_sizing_flat_dollar.md` for the caveat). Real account balance: **$25**.

## Context (from production audit, 2026-05-28)
- 🔴 Engine runs the WRONG strategy: `engine.py:89` calls `run_strategy` (joint_kelly, a known loser); `run_wing_strategy` is never called outside backtests. No dispatch exists.
- 🔴 Same-day anchor blocked: fetchers can get real-time obs, but `latest_viable_anchor` (`data.py:131`) AND-gates on IEM-lagged TAF/ASOS → anchor lands ~2 days back (would trade a settled market).
- 🟡 Live fills faked: `execution.py:149` books limit=fill, no `/portfolio/fills` poll; `get_balance`/`get_positions` are dead code. API base is PROD (no sandbox).
- Sizing is Kelly-only, hardcoded inline per strategy (no flat-$ hook).
- `$25` caps downside → low-stakes live-learning deployment; fee/rounding drag at small scale is the real risk.

## Phase 0 — Validate economics at $25 — DONE, PASSED ✅
- [x] Re-sim @ $25: net-positive at all flat-$ levels; fee drag ~4–4.7% (vs 2.6% @ $1000), not fatal
- [x] Found 3–5/40 fee-dead trades (fee >= win) → added fee-aware gate + tighter max_ask_sum 0.90
- [x] Picked flat $2.50/trade (~10% of $25)

## Phase 1 — Wire strategy + sizing — DONE, VERIFIED ✅
- [x] config.py: WingCfg + StrategyCfg.name; load_config parses strategy.wing
- [x] engine.py: _dispatch_strategy routes name → run_wing_strategy(market_wing, drop_lower_ask, flat-$)
- [x] strategy.py: flat_stake_usd sizing branch + fee_aware gate (Kelly path unchanged — verified)
- [x] config.yaml: name=market_wing, bankroll_usd=25, wing block (flat_usd 2.5, max_ask_sum 0.90, fee_aware)
- [x] Verified end-to-end @ $25: 24 fires, 96% WR, +$7.98 (+32%), −7% maxDD, $2.33 fees
- note: Kelly-path "regression" was a false alarm (bankroll $1000→$25, uniform scaling), not a logic break

## Phase 2 — Model-free engine path — DONE, VERIFIED ✅ (replaced the old weather-anchor plan)
- [x] Confirmed market_wing is model-free → same-day weather anchor (old P3 / Synoptic) is OBSOLETE
- [x] `ModelCfg.enabled` flag; `run_cycle` skips refresh/features/predict; uniform placeholder PMF
- [x] `_dispatch_strategy` routes config `strategy.name` → `run_wing_strategy`
- [x] Verified end-to-end: model-free paper cycle on live KXHIGHCHI (6 contracts, no creds), correct skip on near-settled market

## Phase 3 — Orderbook data logger — BUILT ✅ (deploy outstanding)
- [x] `scripts/orderbook_logger.py`: keyless, resilient, all open events (handles 2-open), per-event-date folders, zip-on-settlement
- [x] `deploy/orderbook-logger.service` + `deploy/README.md` (always-on VM/Pi)
- [ ] DEPLOY: provision a free/cheap always-on host, `systemctl enable --now` (survives laptop shutdown)

## Phase 4 — Go live (small, $25)
- [ ] `.env`: `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` (no weather/Synoptic token needed — model-free)
- [ ] `mode: live` (bankroll_usd already 25); confirm KILL_SWITCH works
- [ ] Run the engine at the 1 PM anchor on an always-on host (`main --loop` / TUI / cron)
- [ ] First real trade; observe fill vs expected

## Phase 5 — Harden (deferred; low-$ impact at $25)
- [ ] Poll `/portfolio/fills` + `/portfolio/balance`; replace the limit=fill stub
- [ ] Enforce `per_anchor_max_trades`; gate on open intraday exposure

---

# Engine review 2026-06-02 — fix implementation (in place, strat_prod_1, no commit/deploy)

Source: `tasks/engine_review_2026-06-02.md`. LIVE code (Chicago bot armed). Preserve 28
passing tests; K=1 behavior identical. Designs in the user brief override the report.

## WARN (correctness, with tests) — ALL DONE ✅
- [x] **W1 — per-cycle order budget.** `RunBudget` in `run_cycle` threaded into each `execute()`.
      NOTE: with W2 atomicity, cap=6 + two 4-leg wings places **4** (one full wing), not the
      report's "6" (a wing can't be split to consume the last 2). 4≠8 proves the cap is shared.
- [x] **W2 — atomic wing vs risk gates.** `_gate_wing` checks the FULL wing once; places all or
      none. Mid-wing leg error after ≥1 fill → `_flatten_legs` (sell @ 1¢ marketable) + remove
      from Book; flatten fail → CRITICAL + keep in Book.
- [x] **W3 — unique coid + partial cancel.** coid `+ run_utc_ms`; partial → cancel remainder.
- [x] **W4 — real fee/PnL where reliable.** positions parse + ack-fee-when-fill_count-matches; TODO left for RECON.

## INFO — DONE ✅
- [x] **I1** doc inline. [x] **I2** cycle-level daily-loss gate (account-wide on any breached date).
- [x] **I3** reuse cycle snapshot. [x] **I4** doc cross-tz bucketing. [x] **I5** untouched. [x] **I6** DST policy.

## Self-review findings (acted on)
- Flatten SELL was `limit 0` (Kalshi rejects 0; range 1–99) → fixed to `1` (still marketable).
- Flatten left Book showing phantom-open legs → added `Book.remove_position` so Book matches exchange.
- I6 prevents the throw but does NOT fix a pre-existing transition-DAY 1-hour drift in the LOG-only
  `t_utc` (normalize()+Timedelta vs wall-clock); cosmetic, out of scope, not worsened.

## Result: 39 passed (28 original + 11 new). No commit, no deploy.

## API field facts (context7-verified, /openapi/kalshi_openapi_yaml)
- `/portfolio/positions` MarketPosition: `realized_pnl_dollars`, `fees_paid_dollars` (req, $ strings).
- CreateOrderResponse: order nested under `order`; `taker_fees_dollars`, `maker_fees_dollars`,
  `fill_count_fp`, `status` (resting|canceled|executed), `order_id` under `order`.
