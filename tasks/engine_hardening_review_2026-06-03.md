# Independent adversarial review — `engine-hardening` (HEAD `dfd7370`) — 2026-06-03

**Reviewer:** independent second-pair-of-eyes (did NOT read `tasks/_agent_bus/**`; verified every
finding against code at file:line). **Scope:** the `strat_prod_1..engine-hardening` diff
(+339/−167 on the order path) implementing W1–W4 + I1–I6 of `tasks/engine_review_2026-06-02.md`.
Files read in full: `execution.py`, `engine.py`, `positions.py`, `kalshi.py`,
`tests/test_multimarket.py`, `scripts/check_live_execution.py`, plus `strategy.py` (TargetPosition /
wing sizing), `config/{live,paper}.yaml`, `report.py`. **No code changed.**

## Verdict

**Counts: 0 CRITICAL · 1 WARN · 4 INFO.**

Tests: `pytest` 39/39 green; `scripts/check_live_execution.py` ALL PASS (re-run, not trusted from
the author). The big rewrite — `execute()` split into **plan → gate-whole-wing-once → place-all-or-none**
— is **correct**: the kill / budget / daily-outlay / exposure gates are all evaluated once against
the *full* leg set in `_gate_wing` before any placement, and the placement loop contains **no**
mid-wing cap/kill re-check (the only `break` is `leg_error`, which fires the flatten). I could not
construct any path where a *gate trip* leaves a naked wing. W1 (shared `RunBudget`), W3 (unique
coid + exchange-truth delta + partial-fill cancel), and W4 (ack-fee field names + match logic) are
all verified correct, including against the official Kalshi OpenAPI (`taker_fees_dollars`,
`maker_fees_dollars`, `fill_count_fp` are real fields nested under `order`).

**The one WARN is a flatten-quantity bug** (`_flatten_legs` + `Book.remove_position`) that is
*latent* under today's single-LIVE-city / flat-$2.50 config but is a genuine Book↔exchange
divergence — and it defeats the flatten's own purpose — on the **top-up** path that W3 newly made
reachable. It is narrow (needs a mid-wing `place_order` exception on a wing whose earlier leg had a
pre-existing holding) and the fix is ~3 lines. **My recommendation: safe to deploy as-is for the
current K=1 / fresh-wing reality, but fix W-A before relying on the flatten safety-net or adding a
2nd LIVE city.** It is not a CRITICAL because a *gate trip* (the common W2 path) is fully correct;
the divergence only arises on the rarer error-driven flatten of a *topped-up* leg.

---

## WARN

### W-A. `_flatten_legs` sells only this-cycle's DELTA but `remove_position` deletes the WHOLE position — a top-up flatten leaves a naked leg on the exchange AND under-counts the Book

**Files:** `weather_alpha/execution.py:279-291` (`_flatten_legs` SELL `count=f["contracts"]`),
`weather_alpha/positions.py:139-150` (`remove_position` `del self.positions[key]`). **NEW (introduced by this branch's W2 flatten + W3 partial-recovery interaction).**

`placed_this_wing` holds the `fill` dicts whose `"contracts"` is the **delta filled this cycle**
(`filled = max(0, after_qty - before_qty)` at `execution.py:348,387`), *not* the absolute holding.
`_flatten_legs` then sells exactly that delta:

```python
await client.place_order(..., action="sell", count=f["contracts"], limit_price_cents=1, ...)
book.remove_position(f["ticker"], f["side"])   # deletes the ENTIRE position
```

But `book.add_fill` only ever added the delta to a position that, on a top-up, **already held the
prior contracts** (booked by a previous cycle's `add_fill`). So after `add_fill` the Book position
is `prior + delta`, while `remove_position` unconditionally `del`s the whole thing.

**Concrete repro (verified dynamically, not hypothesized):** exchange + Book already hold **3** of
leg-A (a prior partial / top-up). This cycle targets leg-A=5 (top-up +2) and leg-B=3; leg-A's +2
fills, then leg-B's `place_order` raises. Result observed:

| | Exchange | Book |
|---|---|---|
| after top-up fill | 5 | 5 |
| flatten SELL `count` | −2 | — |
| `remove_position` | — | delete → 0 |
| **net** | **3 (still held, NAKED)** | **0** |

So the flatten **leaves 3 naked directional contracts on the exchange** — the exact tail exposure
the flatten exists to remove — *and* the saved Book reports 0, diverging from the real 3. That Book
figure then feeds `exposure_cents()` (the C2 cap) and the drawdown HWM, both of which now
under-count real open risk. The CRITICAL log line even claims "removed from Book … Book now flat
for this leg, like the exchange" — both halves are false on a top-up.

Secondary (same family): `remove_position` does `fees_paid_cents -= p.total_fees_cents`, and on a
top-up `p.total_fees_cents` includes the **prior cycle's** fee too, so the running fee total
under-counts by the prior leg's fee. Minor vs the naked-leg issue.

**Why it's latent today (so not CRITICAL):** the common flatten is a *fresh* wing (`before_qty=0`),
where `fill["contracts"] == position.contracts`; then the SELL offsets the full holding and
`remove_position` deletes a genuinely-zero-on-exchange position — fully consistent (this is what
`test_mid_wing_leg_error_flattens_filled_legs_and_squares_book` asserts, and it passes). The bug
needs (a) a multi-leg wing, (b) an earlier leg with a pre-existing holding (the W3 partial-recovery
path), and (c) a *later* leg's `place_order` to raise — uncommon but not negligible over months of
live trading, and the `place_order` exception (network/5xx/timeout) is precisely the
`_signed_request`-exhausted case the flatten was built for.

**Fix direction (any one):**
1. Sell the **full** booked holding, not the delta: read `book.open_for(ticker, side).contracts`
   inside `_flatten_legs` and use that as the SELL count (then `remove_position` is consistent). This
   restores both invariants (no naked leg, Book matches exchange). Simplest + correct.
2. Or only flatten the **delta** AND shrink the Book by the delta (a `Book.reduce_position(ticker,
   side, qty)` that subtracts contracts + pro-rata fees instead of deleting) — this offsets only
   *this cycle's* add while leaving the legitimately-prior contracts intact on both sides. Choose
   this if the prior holding should be *retained* (it was a deliberate earlier fill), not unwound.
   Note the two options encode different intents — (1) unwinds everything for this ticker, (2)
   unwinds only the just-placed delta; (1) matches the "square the wing" framing best.

Add a flatten test with a **non-zero pre-existing holding** (the existing flatten tests all start
from `before_qty=0`, which is exactly why this passed CI).

---

## INFO

### I-A. `check_daily_loss` now scans ALL dates → a single >cap historical day latches the bot OFF permanently, with no reset path

**File:** `weather_alpha/execution.py:459-472` (loops `for date_iso, daily in book.daily_loss_cents.items()`), called once/cycle at `engine.py:116`. **Behavior change vs `strat_prod_1`.**

The old `_check_daily_loss` checked only the **current anchor date** (`book.daily_loss_cents.get(date_iso, 0)`),
so a breach self-limited to that one calendar day's cycles. The new `check_daily_loss` raises if
**any** date in the (permanently-accumulating, never-cleared) `daily_loss_cents` map is `<= -cap`.
There is **no reset** for `daily_loss_cents` (unlike the drawdown latch, which has `reset_halt` +
`scripts/halt.py --reset`). So the first calendar day whose realized loss exceeds
`daily_max_loss_usd` ($15) gates **every future cycle forever** with only a log line — recoverable
only by hand-editing the Book JSON.

**Why INFO, not WARN:** under the current config the trigger is near-impossible. Max same-day
stake-at-risk ≈ `total_exposure_max_pct·$25 = $15` (C2), and a *realized* loss of ≤ −$15 needs the
**entire** coverage wing to settle worthless — which the wing is specifically designed to avoid (a
hedged wing almost always returns ~$1/contract on the winning leg). So `daily_loss_cents[d] <= -1500`
is a tail-of-the-tail event today. It becomes real if `daily_max_loss_usd` is lowered, stakes rise,
or exposure is uncapped. **Recommend:** either key the gate to recent dates only (e.g. last N days)
or give `daily_loss_cents` a documented reset in `scripts/halt.py`, and note in the docstring that
this is now a permanent-until-reset latch, not a per-day soft gate.

### I-B. A flatten-only cycle does NOT re-sync the live bankroll (gated on `if total_realized`)

**File:** `weather_alpha/engine.py:142-144`. The post-cycle `_resync_bankroll_after_settlement` runs
only when `total_realized != 0`. A mid-wing error that triggers `_flatten_legs` realizes a small
round-trip loss (spread + 2× fee) **on the exchange** but books **no settlement**, so
`total_realized == 0` and the bankroll is not re-queried that cycle. `book.bankroll_cents` (the
sizing + cap base) therefore stays stale until the next *settlement* cycle re-syncs it. Bounded
(a few cents) and self-healing, but worth a one-line note; if W-A is fixed by selling the full
holding, consider also forcing a re-sync when a flatten occurred.

### I-C. `_gate_wing` budget message can read oddly when a prior city already consumed the budget

**File:** `weather_alpha/execution.py:240-243`. The log says `budget=%d, used=%d … wing won't fit`
using `budget.cap` and `budget.attempts`. Correct numerically, but for the 2nd city in a cycle
`attempts` reflects the **prior** city's placements, so an operator reading the line in isolation
may misattribute the "used" count. Cosmetic; the `remaining()` math is right (verified by
`test_per_cycle_order_budget_sums_across_markets`: cap 6, CHI places 4, NYC's 4-leg wing sees
remaining 2 → skips → total 4, asserted exactly).

### I-D. Ack-fee `fill_count` match is a strict equality on two differently-sourced counts — usually falls back to the (safe) estimate

**Files:** `weather_alpha/execution.py:379-383`, `kalshi.py:334-351`. `order_ack_fee_cents` returns
the ack's `fill_count_fp` (fills **at ack time**); the booked `filled` is the *position-poll* delta
up to 3 s later. When the resting portion fills *after* the ack, `ack_fill < filled` → equality
fails → formula estimate is used. This is the **safe** direction (never books a fee for the wrong
quantity), correctly documented, and asserted by `test_live_books_exchange_reported_fee_when_present`
(synchronous full fill → counts match → exchange 4c stored over the 6c estimate). Flagged only so
it's understood the ack-fee path will *often* not fire on a real partial/late fill — the formula
estimate remains the common case, exactly as the W4 TODO notes (true per-position fee deferred to a
`/portfolio/positions` reconciliation, HANDOFF RECON).

---

## Things specifically verified CORRECT (claims that held up)

- **W2 atomicity (gate path):** `_gate_wing` (`execution.py:223-263`) evaluates kill > budget >
  daily-outlay > exposure once on the *whole* wing's `Σ delta_cost_cents`; if any binds, **every**
  planned leg is skipped (`execution.py:146-151`) and `placed == []`. The placement loop
  (`:159-186`) has **no** mid-wing cap/kill re-check — the only `break` (`:171`) is `leg_error`.
  Confirmed no naked-wing path from a gate trip. Threshold math uses the same `>` operator and
  cents/dollars units as the old per-leg code (`cap_cents = round(daily_max_loss_usd*100)`;
  `exp_cap_cents = round(total_exposure_max_pct*bankroll*100)`), just summed — no off-by-one.
- **W2 kill-switch sharpest case is now FIXED, not regressed:** arming between leg 1 and leg 2 used
  to skip leg 2 (naked leg 1); now the wing is gated up-front or placed in full. Tests
  `test_wing_atomic_kill_armed_{before,midcycle}` assert `placed == []`.
- **W1 RunBudget:** created fresh per `run_cycle` (`engine.py:107`), threaded into every
  `execute()`, decremented once per attempt (`execution.py:160`). Shared across cities, reset per
  cycle, never leaks. `remaining()` clamps at 0. Asserted shared (4≠8) by
  `test_per_cycle_order_budget_sums_across_markets` and not-over-restrictive (8) by the companion.
- **W3 double-BUY impossible:** every live placement is sized as `delta = target − current_qty`
  where `current_qty` comes from the cycle-start `get_positions` snapshot (`execution.py:96-99,128-134`).
  A re-run/retry takes a *fresh* snapshot → delta shrinks → no over-buy. The unique-per-attempt coid
  (`coid = wa-{date}-{ticker}-{side}-{run_utc_ms}`, `:328`) only removes the false-idempotency
  crutch; the exchange-truth delta is the actual gate. Within a cycle, wing legs are distinct
  tickers so leg-1's fill never staleness-inflates leg-2's delta.
- **W3 partial-fill remainder cancel:** targets the just-placed `oid` (`:361,371`) — the correct
  order. If it fully fills in the race, the cancel errors harmlessly (`_cancel_quietly` swallows)
  and the next cycle's exchange-truth delta corrects sizing; the Book under-counts for one cycle
  but never over-buys. `test_partial_fill_cancels_remainder_and_unique_coid_allows_completion`
  asserts book 2→3 across cycles with `coid_1 != coid_2`.
- **W4 ack-fee field names are REAL (not guessed):** verified against the official Kalshi OpenAPI
  (docs.kalshi.com/api-reference/orders/create-order): `taker_fees_dollars`, `maker_fees_dollars`,
  `fill_count_fp` exist and are nested under `order`. `_parse_positions`' `realized_pnl_dollars` +
  `fees_paid_dollars` are confirmed real `/portfolio/positions` fields (HANDOFF `TUI`). Fee math is
  dollars→cents (`round(_fp(x)*100)`), consistent. Match logic uses the ack fee only on exact
  `ack_fill == filled` (safe), else the formula estimate.
- **`_flatten_legs` SELL side/price (fresh-wing case):** `action="sell", side=f["side"]` correctly
  offsets a long position you bought with `action="buy", side=...`; `limit_price_cents=1` is a
  marketable sell (crosses any bid ≥1¢, fills at the bid by price-time priority; 1 not 0 because
  Kalshi prices are 1–99¢). Correct for the common (non-top-up) case. Flatten failure KEEPS the leg
  in the Book and logs CRITICAL, never re-raises, so `book.save` still runs
  (`test_mid_wing_flatten_failure_keeps_leg_in_book`).
- **No-regression on the prior-correct behavior:** at-target / not-live / no-target filtering and
  their skip rows are preserved verbatim in the plan phase (`execution.py:116-138`); per-station
  settlement (`reconcile_settlements(station=…)`) unchanged; LIVE marginal top-up price
  (`marginal = (after_qty*after_avg − before_qty*before_avg)/filled`, `:352`) unchanged and asserted
  by `test_live_books_marginal_price_on_topup`; `daily_outlay_cents` still excludes settled
  (`positions.py:134`); drawdown latches before the loop + after each market (`engine.py:103,137,144`);
  `Book.save` still tmp+atomic-replace (`positions.py:247-252`); `book.save` runs unconditionally
  after placement even on `leg_error` (`execution.py:193`).
- **Tests assert the all-or-none INVARIANT, not just "it runs":** the W2 tests assert
  `cli.placed == []` (zero orders submitted) plus `r.skipped == N`, which is the load-bearing
  assertion. `fees_paid_cents == 0` / `exposure_cents() == 0` after a fresh-wing flatten is asserted
  too. (Gap: no test exercises a *top-up* flatten — see W-A.)

---

## Deploy recommendation

**Concur the branch is functionally safe to deploy under the current K=1 LIVE (Chicago) /
flat-$2.50 / fresh-wing reality** — the W2 atomic-gate rewrite is correct and is a strict
improvement on the prior per-leg break semantics. **Before** relying on the flatten safety-net in
anger or adding a 2nd LIVE city, fix **W-A** (≤3 lines: sell the full booked holding, or add
`Book.reduce_position`) and add a non-zero-pre-existing-holding flatten test. I-A (permanent
daily-loss latch) is worth a follow-up for defense-in-depth but is near-unreachable under the
present config.
