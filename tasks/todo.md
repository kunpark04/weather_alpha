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
