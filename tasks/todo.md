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

## Phase 2 — Same-day anchor
- [ ] Set `SYNOPTIC_TOKEN` (or accept `metar_substitute`)
- [ ] Rework `latest_viable_anchor` to accept "today" from real-time sources (drop/relax IEM-lagged TAF+ASOS AND-gate)
- [ ] Confirm merged ASOS parquet reaches today's 1 PM after a live refresh

## Phase 3 — Paper dry-run
- [ ] Run wired bot in paper for a few days; confirm same-day prediction + would-be order
- [ ] Eyeball Kalshi liquidity for a handful of contracts at the modal+adjacent

## Phase 4 — Go live (small)
- [ ] Populate `.env`: `KALSHI_KEY_ID`, `KALSHI_PRIVATE_KEY_PATH` (+ `SYNOPTIC_TOKEN`)
- [ ] `mode: live`, `bankroll_usd: 25`, confirm KILL_SWITCH works
- [ ] Dry-run `--headless --no-refresh` against a recent anchor; eyeball the order
- [ ] First real trade; observe fill vs expected

## Phase 5 — Harden (deferred; low-$ impact at $25)
- [ ] Poll `/portfolio/fills` + `/portfolio/balance`; replace limit=fill stub
- [ ] Enforce `per_anchor_max_trades`; gate on open intraday exposure
