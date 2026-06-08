# Engine code review — adversarial correctness audit (2026-06-02)

**Scope (read in full, this pass):** `weather_alpha/engine.py`, `execution.py`, `strategy.py`,
`positions.py`, `kalshi.py`, `scheduler.py`, plus `config.py`, `pmf.py`, `fees.py`, `live_log.py`,
`main.py`, `tui.py` (entry/driver checks), `config/{live,paper}.yaml`,
`deploy/weather-alpha-{live,paper}.service`, and `tests/test_multimarket.py` (28 pass).
Read-only — **no code changed**. The brief: find CORRECTNESS bugs in the LIVE trading path, with
priority on double-trade/double-fill, multi-market cross-contamination, settlement correctness,
risk-gate bypass/double-count, money math, and persistence.

This builds on `tasks/engine_code_review_2026-06-01.md` (2 CRITICAL · 7 WARN · 8 INFO + 9 follow-up,
all marked fixed). I re-verified each prior fix is still in place (none regressed) and looked for
NEW issues. The report below is only the new/missed findings + a short regression confirmation.

## Verdict

**Counts: 0 CRITICAL · 4 WARN · 6 INFO.**

The core money path is sound. I specifically re-derived and confirmed: Kalshi fee
`ceil(0.07·N·P·(1−P))` charged at entry only; equal-payout `n_i = int(K)` per leg; settlement
win/loss (`gross_per = 100 − avg_cost` win / `−avg_cost` loss, fees subtracted, daily-loss sign);
the per-station settlement filter (no cross-city contamination); the exchange-truth delta
(`get_positions` keyed by ticker, tickers are city+date unique → no key collisions); the LIVE
**marginal** top-up price (verified against Kalshi's `market_exposure_dollars` = "Cost of the
aggregate market position", i.e. cost basis of open contracts, so `avg_price_cents` is a true
per-contract cost and the back-out at `execution.py:287` is correct); the exposure cap
(`book.exposure_cents()+delta ≤ pct·bankroll`, units consistent: cents vs `pct·$·100`); Book JSON
atomic round-trip; and that a mid-cycle kill/cap trip still persists already-filled legs and still
runs settlement + the bankroll re-sync.

**No new CRITICAL.** The four WARNs are correctness gaps that are **bounded today** by the
single-LIVE-city / flat-$2.50 configuration but become real money bugs the moment a 2nd LIVE city
is added or stakes rise — exactly the regime the multi-market refactor was built for. W1 (per-anchor
cap is per-market, not per-cycle) and W2 (a mid-wing cap/kill break leaves a permanently lopsided,
unhedged wing for the day) are the two to fix before a 2nd live city.

---

## WARN

### W1. `per_anchor_max_trades` is enforced per-market, not per-cycle — the cumulative order backstop scales with city count
**Files:** `weather_alpha/execution.py:92` (`attempts = 0` is a local), `:141`; `weather_alpha/engine.py:108` (one `execute()` per market). **NEW.**

`attempts` is a function-local counter inside `execute()`, and `execute()` is invoked **once per
market** by `_run_one_market`. So `cfg.risk.per_anchor_max_trades` (=6 in both live.yaml and
paper.yaml) caps order attempts **per city per cycle**, not across the whole anchor cycle. With K
cities firing in one `run_cycle`, the true ceiling on submitted orders is **K × 6**, not 6.

The name (`per_anchor_max_trades`) and the prior review's framing ("runaway-order backstop", "the
real per-anchor backstop") both read as a **single** per-anchor ceiling. It is not. Today K=1 LIVE
(CHI) so it happens to equal 6; the PAPER bot already runs K=2 (HOU+CHI) → effective 12. Because
each city's wing only emits 2–3 legs, the cap never actually binds, so this is latent — but it is a
mislabeled safety limit: a strategy bug that emitted a runaway target list in one city would still be
capped at 6 for *that* city, yet the documented "per-anchor" guarantee implies the process as a whole
is bounded at 6, which is false.

**Why it's a bug, not a nit:** the cap is a money-safety backstop; its documented contract
(per-anchor) does not match its behavior (per-market). A reader sizing the worst-case order flow off
this number will under-estimate it by the city multiplier.

**Fix direction:** decide the intended semantic and make code match docs. If it is meant per-city,
rename to `per_market_max_trades` and say so. If it is meant per-cycle, thread a shared attempt
counter (or a small `RunBudget` object) through `run_cycle` into each `execute()` so the cap sums
across markets — mirroring how `book.exposure_cents()` / `daily_outlay_cents()` already sum
process-wide. Add a multi-market test asserting the cumulative ceiling.

### W2. A mid-wing cap/kill `break` leaves a permanently lopsided, unhedged wing for that day (no same-day retry)
**Files:** `weather_alpha/execution.py:130-181` (`break` on kill / max_trades / daily_outlay / exposure after ≥1 leg filled), `scheduler.py:189-199` + `main.py:136-148` (each market fires **once per local day**). **NEW (interaction).**

The `market_wing` thesis is *coverage*: hold the modal bucket **and** its kept adjacent so that
whichever resolves, the wing is hedged. In `execute()` the legs are placed sequentially; if a risk
gate trips **after** the first leg has filled (kill-switch armed mid-cycle, `per_anchor_max_trades`,
`daily_outlay_cap`, or the `exposure_cap`), the code records the remaining leg(s) as skipped and
`break`s. The already-filled leg is correctly persisted — but the position is now a **single
directional leg**, not a hedged wing.

Crucially there is **no same-day retry to complete it**: `MarketAnchorScheduler.due_markets` fires a
city at most once per local date and records it, so the bot will not re-enter that market until the
**next** day's (different) event. The exchange-truth delta would happily top up the missing leg on a
later cycle, but no later cycle for that event ever runs. Result: for the rest of the day the city
holds an unhedged single-bucket bet — precisely the tail exposure the wing exists to avoid.

This is bounded today (flat $2.50; caps on the $25 account are generous so a mid-wing trip is
unlikely with K=1), but it is a real correctness gap: a cap/kill trip is supposed to *reduce* risk,
yet here it can convert a hedged 2-leg wing into a naked 1-leg position.

**Why it's a bug:** the failure mode silently changes the strategy's risk profile (hedged → naked)
rather than cleanly aborting or completing. The kill-switch case is the sharpest: an operator arming
the switch between leg 1 and leg 2 *increases* directional exposure for the day.

**Fix direction:** treat the wing as atomic w.r.t. the risk gates — evaluate the *whole* wing's
incremental cost against the caps **before** placing any leg (pre-check `Σ delta_cost` for the
target set), so the cycle either places the full hedge or none of it. For the kill-switch, that means
checking it once up-front for the whole leg set is insufficient given the per-leg semantics you want;
the cleaner contract is "place the complete wing or skip it." At minimum, on a mid-wing break, either
immediately flatten the orphaned leg or flag the city for a same-day completion pass.

### W3. Deterministic `client_order_id` + partial fill: remainder can never complete (still unverified against a live partial)
**File:** `weather_alpha/execution.py:268` (`coid = f"wa-{anchor_date}-{tgt.ticker}-{tgt.side}"`), `:271-307`. **CARRIED FORWARD from 2026-06-01 W3/follow-up — re-confirmed still open, and the partial path is now reachable on a real account.**

On a LIVE partial fill (e.g. 2 of 3), the Book records 2; the next cycle would compute `delta=1` and
re-`place_order` with the **same** `coid` and `count=1`. Kalshi rejects a duplicate
`client_order_id`, so the remainder is never re-submitted — and per W2 above there is no same-day
retry anyway, so the partial wing stays partial for the day. The prior review consciously chose this
("safe under-fill over double-order") and deferred it pending a live partial-fill test; I am
re-listing it because (a) it is now **live-reachable** (the bot is armed, first real fill imminent),
(b) it compounds with W2 into "partial wings cannot self-heal within the day", and (c) there is still
no mock-client partial-fill test (`tests/test_multimarket.py` covers no-fill cancel and full-fill
top-up, but not a 2/3 partial).

**Why it's a bug:** the only safe behaviors are (i) place the remainder under a *new* idempotency
key, or (ii) cancel-replace the resting order for the true remaining qty. Today's behavior leaves the
wing under-covered with no recovery path until the next day.

**Fix direction:** make the `coid` unique per attempt while preserving idempotency per logical intent
(e.g. append a monotonic cycle/attempt token), OR cancel any resting remainder and re-place for the
real outstanding qty. Add a partial-fill mock test that asserts the remainder is eventually filled
(or the resting order is cancel-replaced), not silently dropped.

### W4. LIVE per-position fee is the formula estimate, not the exchange-charged fee — Book fee/PnL fields drift from truth
**Files:** `weather_alpha/execution.py:316` (`"fee_cents": trade_fee_cents(price_cents/100, filled)`), `kalshi.py:296-320` (`_parse_positions` surfaces no fee / `realized_pnl_dollars`). **CARRIED FORWARD (HANDOFF AUDIT #8 / prior W2) — re-confirmed; the exchange now exposes the real fields.**

On a confirmed LIVE fill the booked fee is the bot's `ceil(7%·N·P·(1−P))` *estimate*, not Kalshi's
actually-charged fee; the two can differ by rounding/schedule nuance. So `Book.fees_paid_cents`,
per-position `realized_pnl_cents`, and `exposure_cents()` (uses `avg_cost`) drift from the true
account. The post-settlement `get_balance()` re-sync (`engine.py:361-372`) corrects the **cash
balance**, but the per-position fee/PnL fields — which feed the drawdown HWMs and the exposure cap —
stay estimates. The Kalshi `/portfolio/positions` payload now carries `realized_pnl_dollars` and the
order/fills endpoints carry the real fee, so this is fixable rather than fundamentally unknowable.

**Why it's a bug (low severity):** the drawdown halts and exposure cap gate on Book-internal realized
PnL / exposure, which are fee-estimate-based; a systematic fee mismatch biases those gates slightly.
Immaterial at flat $2.50 on $25 against a 25%/50% drawdown and a 60% exposure cap, but it is a real
truth-vs-estimate divergence on the live path.

**Fix direction:** read the fill fee from the order ack / fills endpoint and book that; extend
`KalshiPosition` to carry `realized_pnl_dollars` + `fees_paid_dollars` (also required for the
read-only-monitor TUI refactor). Until then, keep relying on the balance re-sync for the cash figure
and treat Book fee fields as approximate.

---

## INFO

### I1. Exposure & outlay caps measure already-open stake at `avg_cost` but charge the new leg at `limit_price` — slight asymmetry
`execution.py:155` (`delta_cost_cents = delta_qty * limit_price_cents`) vs `positions.py:105,111`
(open stake summed at `avg_cost_cents`). The incoming leg is costed at its limit while existing stake
is costed at realized average. For a marketable taker order these are within a cent or two, so the
caps are off by at most the spread on the marginal leg. Harmless; just not apples-to-apples.

### I2. `_check_daily_loss` raises per-market but the cap is account-wide — all cities gate together by luck, not design
`execution.py:75,375-387`, caught per-market at `engine.py:193-200`. `daily_loss_cents[date]` is a
single account-wide accumulator, so each city's `execute()` sees the same value and all cities gate
identically when it trips — the intended account-wide behavior emerges, but via N independent
per-market raises rather than one cycle-level check. (Moot in practice: as the prior W5 noted, this
gate is ~always 0 for the current anchor since realized loss only accrues at next-day settlement.)
Consider a single cycle-level check for clarity.

### I3. Two independent `get_positions` snapshots per LIVE market cycle (delta vs fill-confirm) — extra calls + a tiny race window
`execution.py:84` takes one snapshot for the delta decision; `_execute_one` (`:270,282`) takes
fresh before/after snapshots per leg via `_live_held`. For a 2-leg wing that is up to 1 + 2×2 = 5
`/portfolio/positions` calls. Because wing legs are distinct tickers there is no key overlap and no
correctness issue, but the top-level delta snapshot can be stale by the time a later leg places. Low
impact (the per-leg before/after is the authority for what booked); flagged only as call-count +
staleness, not a bug.

### I4. `daily_outlay_cents` / `exposure_cents` both count cross-city stake for the SAME calendar date (HOU + CHI share `America/Chicago`)
`positions.py:107-113`, `:103-105`. Both sum process-wide, which is the intended cumulative behavior
for the shared Book. Note that paper.yaml's two cities share a tz, so they also share one
`anchor_date` string — the outlay cap is genuinely cumulative across them (good). For cities in
**different** tz the same-calendar-date `anchor_iso` can differ by a day at the UTC boundary, so the
outlay cap (keyed by `anchor_date`) buckets them separately while the exposure cap (un-keyed) still
sums them. Consistent with intent; documented here so a future cross-tz LIVE pair isn't a surprise.

### I5. Settlement matches `anchor_date` to the CLI **observation** date — correct, and explicitly avoids the API `settlement_date` off-by-one
`execution.py:463-491`, `engine.py:390-404`, `live_fetchers.py:162-170`. Verified the project's
known off-by-one (Kalshi `settlement_date = strike_date + 1`) is **not** present: positions store
`anchor_date = str(prediction.date.date())` (the event/strike date) and `reconcile_settlements` keys
the CLI truth by observation `date`, matching strike-date to the day the high was measured — the
correct join for a daily-high market. No finding; recorded as a verified-sound area.

### I6. `_t_utc_for` localizes the anchor with `tz_localize(tz)` without a DST `ambiguous`/`nonexistent` policy
`execution.py:394-396`. On a US "spring-forward" date 1 PM local is well clear of the 02:00 gap and 1
PM is never an ambiguous fall-back hour, so for the 13:00 anchor this never throws. It would only bite
if `anchor_hour_local` were moved into a DST transition hour. Cosmetic for the current 1 PM anchor.

---

## Prior-review fixes re-verified present (no regression)

Spot-checked each 2026-06-01 resolution against current code:

- **C1** kill/halt require `--config` and load `require_live_creds=False` — `config.py:180-185`,
  and the live bot reads `cfg.paths.kill_switch = data/live/KILL_SWITCH` (`execution.py:364`,
  live.yaml:25). Present.
- **C2** exposure cap enforced in `execute()` — `execution.py:168-181`; `daily_max_loss_usd` is 15
  (live.yaml:82). Present.
- **#3** LIVE deltas size off exchange truth — `execution.py:81-86,116-121`. Present.
- **#4** unfilled LIVE order cancelled — `execution.py:292-304`. Present.
- **#6** top-up books the **marginal** price — `execution.py:285-288` (verified correct against
  Kalshi `market_exposure` = cost basis). Present.
- **#7** `event_ticker` from the API response; **#8** 60-min anchor window — `kalshi.py:170-173`,
  `scheduler.py:30,197`. Present.
- **W1 (old)** `add_fill` opens a fresh position on a settled key — `positions.py:65-81`. Present.
- **W4 (old)** drawdown latches before the loop + after each market — `engine.py:102,120,127`.
  Present.
- **W7 (old)** `daily_outlay_cents` excludes settled rows — `positions.py:113`. Present.
- Kill-switch → settlement-still-runs — `engine.py:193-211`. Present.

**Still-open items the prior review itself deferred (not re-counted above as new):** the TUI remains
an active trade driver (`tui.py:81,153,167` — duplicate single-tz `Scheduler` + `run_cycle`), tracked
as HANDOFF `TUI`; Book↔exchange reconciliation at activation/cycle-start is still unimplemented
(HANDOFF `RECON`). Both are real but already on the books; W3/W4 here overlap the reconciliation work.
