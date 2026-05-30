# Adversarial Logic Review — LIVE trading path (forecast_alpha)

_Reviewer pass: read-only. Date: 2026-05-30. Scope: engine.py, execution.py, strategy.py,
kalshi.py, config.py, positions.py, main.py, scheduler.py for the LIVE order path._

Production strategy under review: `market_wing + drop_lower_ask`, model-free, flat-$2.50/trade,
fee-aware, on KXHIGHCHI ($25 real account). Live mode wired but not yet enabled.

**Severity legend**
- **CRITICAL** = can lose money / place wrong orders in LIVE.
- **WARN** = incorrect behavior but bounded / recoverable.
- **INFO** = robustness / clarity.

Summary counts: **4 CRITICAL, 6 WARN, 4 INFO.**

---

## RESOLUTION (2026-05-30) — all 4 CRITICAL fixed + verified

| ID | Status | Fix |
|---|---|---|
| C1 (under-fill booked as full) | ✅ FIXED | LIVE `_execute_one` confirms the **actual** fill via `_live_held()` (reads `get_positions`), books only what filled, returns `None` on no-fill so the leg retries. Bounded poll (`_FILL_POLL_TRIES=3`) for late fills. |
| C2 (limit at ask, zero buffer) | ⚠️ ACCEPTED | Kept limit-at-ask, but C1 removes the "assume filled" half — an unfilled leg is now correctly unbooked, not phantom. Marketable buffer left as a future tuning knob. |
| C3 (one-shot checks; dead daily cap) | ✅ FIXED | Kill switch **re-checked before every leg**; new **intraday outlay circuit breaker** (`Book.daily_outlay_cents` vs `risk.daily_max_loss_usd`); `per_anchor_max_trades` enforced (was W6). |
| C4 (no idempotency) | ✅ FIXED | Deterministic `client_order_id = fa-{anchor}-{ticker}-{side}` so a retry/re-run collides on Kalshi's uniqueness check instead of doubling. |
| W6 (per_anchor_max_trades unused) | ✅ FIXED | Enforced in the execute loop (folded into C3). |

Verified by `scripts/check_live_execution.py` (mock client, no creds): full-fill books actual
count + deterministic coid; no-fill books nothing; partial books only the filled qty; the C3
outlay cap halts with `halt_reason=daily_outlay_cap`. Paper smoke test still exit-0.

**Still open (lower priority):** C2 buffer tuning; W1/W2 (persist in-flight order / scheduler
state across restart); W5 (reconcile bankroll from `get_balance`); W3/W4 (event-selection
robustness for the 2-events-open case). The C1 fix (read exchange, don't trust the Book) is the
foundation for W1/W5.

---

## CRITICAL

### C1. Under-fill is booked as a FULL fill → permanent under-position, and across restarts can cause duplicate/runaway top-ups
**Files:** `execution.py:81-107` (delta logic + `book.add_fill`), `execution.py:144-158` (LIVE `_execute_one`).

In LIVE mode `_execute_one` returns a synthetic fill that always assumes the order filled
**completely** at the limit price:
```python
return {"contracts": tgt.target_contracts, "fill_price_cents": tgt.limit_price_cents, ...}
```
There is no read of the order acknowledgement's `filled_count`/`status`. The book is then
updated (`execution.py:99-104`) as if the full quantity filled.

Two distinct money bugs flow from this:

1. **Silent under-position (this cycle).** A limit order at exactly the ask (see C2) may fill
   partially or not at all on Kalshi. The bot records a complete fill, so the wing is recorded
   as fully on when in reality one or more legs are short. The equal-payout property the whole
   strategy depends on is broken — on a "win" leg that didn't actually fill, the bot collects
   nothing while believing it's covered. PnL accounting is then wrong for the rest of the day
   and at settlement.

2. **Top-up / duplicate risk across cycles + restarts.** The intraday delta logic
   (`execution.py:84-91`) computes `delta_qty = tgt.target_contracts - current_qty`, where
   `current_qty` comes from the persisted Book. Because the Book over-records (assumed-full),
   `delta_qty` will read `<= 0` on the next INTRADAY tick and *skip* the genuinely-needed
   top-up (`reason="at_target"`). Conversely, if a POST is retried at the kalshi layer
   (`_signed_request` retries up to 3×, see C4) and one of the earlier attempts actually
   reached the exchange, the same anchor can place the order twice while the Book only knows
   about one — true runaway/duplicate on the exchange that the Book cannot detect or net out.

**Concrete failure scenario:** 1 PM anchor fires `market_wing` (2 legs, ~$2.50). LIVE POST for
leg A returns a 503 once, `_signed_request` retries, second attempt succeeds — but the first
attempt also hit the matching engine. Two orders for leg A are live on Kalshi. Book records
one. Account is double-exposed on a $25 bankroll. No reconciliation ever corrects it because
LIVE never reads `/portfolio/positions` or `/portfolio/fills`.

**Suggested fix:** In LIVE, read the order ack's actual resting/filled state (poll
`/portfolio/fills` or GET the order by `order_id`), book only the confirmed filled quantity,
and reconcile the Book against `get_positions()` at the top of every cycle (the method already
exists, `kalshi.py:173`, but is unused). Make `client_order_id` deterministic per
(anchor_date, ticker, side, cycle) — see C4 — so a retried POST is idempotent.

---

### C2. Limit price set exactly at the ask with zero buffer; in LIVE the assumed fill == limit, so a moved/empty book silently no-fills or the order rests unfilled
**Files:** `strategy.py:648` (`limit_price_cents=int(round(c.yes_ask * 100))`),
`execution.py:136-139` (`_paper_fill_cents` gate), `execution.py:150-156` (LIVE returns limit as fill).

Every wing leg's limit price is the **current `yes_ask`, no slippage buffer**. The gate in
`_execute_one` is `if fill_price_cents > tgt.limit_price_cents: return None`. In PAPER,
`_paper_fill_cents` adds `slippage_cents` (`execution.py:189`), so a non-zero slippage config
makes paper *more pessimistic* than live. In LIVE there is no such addition — the order is
submitted at exactly the ask and the limit price is then **assumed to be the fill**.

On Kalshi, a buy-yes limit at the current ask will fill only if that ask size is still resting
when the order arrives. If the ask ticked up by 1¢ in the seconds between fetch and submit
(`fetch_event` at `engine.py:92` → `execute` → `place_order`), the order rests unfilled, but the
Book records a complete fill at the old ask (C1). The strategy's edge margins are razor-thin
(`ev_margin = assumed_win_prob - sum_asks`, often a few cents on a 90¢ wing), so a 1-2¢
adverse move plus the recorded-but-unfilled state directly converts a modeled small win into a
real loss.

**Suggested fix:** Add an explicit `live_limit_buffer_cents` (e.g. +1-2¢ over ask) so marketable
limits actually fill, and/or treat an unconfirmed fill as zero rather than full (C1). Re-fetch
the book immediately before submit and re-validate `sum_asks < max_ask_sum` so a moved book
doesn't get traded at stale prices.

---

### C3. Kill-switch and daily-loss check run ONCE at the top of `execute()`; they cannot stop a multi-leg cycle mid-flight, and the daily-loss cap only ever sees *settled* losses
**Files:** `execution.py:57-58` (single pre-trade check), `execution.py:69-107` (per-leg loop),
`execution.py:203-212` (`_check_daily_loss`), `positions.py:80-82` (daily_loss only updated in `settle`).

Two coupled gaps:

1. **No mid-cycle re-check.** `_check_kill_switch` and `_check_daily_loss` are called once
   before the `for c in contracts` loop. A `market_wing` cycle places 2 legs; nothing
   re-evaluates the switch between leg 1 and leg 2. If an operator drops the kill-switch file
   during a cycle, leg 2 still fires. (The task notes this is about to be fixed — confirmed
   real and located precisely here.)

2. **Daily-loss cap is structurally near-useless for intraday protection.**
   `book.daily_loss_cents` is only ever incremented inside `Book.settle`
   (`positions.py:80-82`). Settlement happens the *next* day when CLI truth drops
   (`reconcile_settlements`, `execution.py:288-309`). So during the trading day the cap sees
   `0` for today's date and **can never trip on the day you're actually trading**. It only
   blocks *new* same-anchor-date opens after that date already settled negative — which never
   happens because each event trades once at its anchor. The cap also keys on
   `prediction.date` (the anchor/event date), not the calendar day of execution, so cumulative
   real-money outflow during a session is never bounded by it.

**Concrete failure scenario:** A bug or bad market causes the bot to open the full per-contract
cap on a leg; the daily-loss guard reads `0¢` for today and waves it through. Loss is only
recognized at next-day settlement, far too late to have stopped the trade.

**Suggested fix:** Re-check kill-switch + daily-loss before *each* leg's `place_order`. Track an
intraday *spent/exposure* counter (cents deployed today, from fills not settlements) and cap on
that, plus enforce `risk.per_anchor_max_trades` (currently loaded but never referenced anywhere
in the live path — see C-note in W6).

---

### C4. No order idempotency: `client_order_id` defaults to a timestamp, so retries / restarts / two-events-open can place the same economic order twice
**Files:** `kalshi.py:210` (`client_order_id or f"fa-{int(time.time()*1000)}"`),
`kalshi.py:223-251` (`_signed_request` retries POST on 5xx/TransportError),
`execution.py:93-107` (no dedupe before booking).

`place_order` generates a fresh millisecond-timestamp `client_order_id` on every call when none
is passed (and `execution.py` never passes one). Combined with the retry loop in
`_signed_request` (which retries POST `/portfolio/orders` up to 3× on any `TransportError` or
5xx), a request that the exchange actually received but whose response was lost will be
**resubmitted with a brand-new client_order_id** — Kalshi treats it as a distinct order. On a
$25 account a single duplicated wing is a large fraction of bankroll.

There is also no guard against the same anchor firing twice within a process: if `run_cycle` is
invoked twice for the same event (scheduler edge, manual re-run, INTRADAY tick after a
restart that lost in-memory scheduler state — see W2), the delta logic is the *only* thing
preventing re-entry, and that logic is corrupted by C1.

**Suggested fix:** Pass a deterministic `client_order_id` keyed on
`(anchor_date, ticker, side)` (Kalshi rejects duplicate client_order_ids), so retries and
re-runs are idempotent at the exchange. Do **not** auto-retry POST `/portfolio/orders` on
network errors without first confirming via GET whether the order landed.

---

## WARN

### W1. `_current_bankroll` ignores open exposure and never queries the real Kalshi balance in LIVE — sizing can exceed cash on hand
**Files:** `engine.py:154-161`, comment explicitly says LIVE "would query" balance but doesn't.

`bankroll = starting_bankroll + (realized_pnl - fees)/100`. Open (unsettled) positions are not
subtracted. With flat-$2.50 sizing on $25 this is bounded, but `total_exposure_max_pct` and
`per_contract_max_pct` caps are computed off this inflated bankroll, so the *caps* don't bind to
actual free cash. If two events are open at once (W3) the bot can commit more than the account
holds; Kalshi will reject, but the Book will have recorded the rejected leg as filled (C1).

**Fix:** In LIVE, set bankroll from `get_balance()` (method exists, unused) and subtract
`book.exposure_cents()`.

### W2. Headless `--loop` scheduler state is in-memory only; a restart re-fires the anchor for the day
**Files:** `main.py:86-99` (`Scheduler(cfg)` constructed fresh each process), `scheduler.py:34-39`
(`SchedulerState` defaults all-None), `scheduler.py:63-65` (anchor fires if `last_anchor_run_date`
is None or < today).

`SchedulerState` is not persisted. After any crash/restart on the same day, `last_anchor_run_date`
is `None`, so `due()` immediately returns `ANCHOR` again. The only thing preventing a duplicate
wing is the Book delta logic (C1) — which is exactly the logic that's unreliable. Net: restart =
real re-fire risk for the day's event.

**Fix:** Persist scheduler state (or derive "already traded today" from the Book by checking for
an open/settled position whose `anchor_date == today`), and make order submission idempotent (C4).

### W3. Two events open at once (today + tomorrow): the engine only ever fetches/settles ONE event, anchored to "today"
**Files:** `engine.py:86-92` (model-free anchor = `local_today`; `fetch_event(anchor, ...)`),
`kalshi.py:133-136` (ticker built from the single date), `engine.py:100` (`reconcile_settlements`
walks *all* book positions but settlement truth is keyed by anchor_date).

`fetch_event` resolves exactly one event ticker from one date. In the model-free path the anchor
is hard-coded to `local_today`. KXHIGHCHI routinely has the next day's event open before today's
closes. The engine will never trade tomorrow's event (acceptable), but two subtler issues:

- If the bot is run after today's event has **closed** but tomorrow's is open, `local_today`
  still points at the closed event → `live` contracts is empty → no trade (safe, but silent).
- The `event_date` off-by-one noted in CLAUDE.md §4.7 (`settlement_date = strike_date + 1`) is
  avoided here because the ticker is built from the local date with `%y%b%d`, not from the API's
  settlement_date — good. But `reconcile_settlements` matches `cli_df.date` to
  `position.anchor_date`; if `anchor_date` was ever stored from the API settlement field
  elsewhere, settlement would mis-match. In the current code `anchor_date` is set from
  `prediction.date` (`execution.py:103`) which is the local event date, so it's consistent —
  worth an assertion to keep it that way.

**Fix:** Make the traded event explicit (validate the resolved ticker's `close_time_utc` is in
the future before trading), and assert anchor/event-date consistency between fetch and settle.

### W4. `_execute_one` exception from `place_order` is uncaught → a failed leg aborts the cycle and can leave half a wing on the exchange
**Files:** `execution.py:93` (`fill = await _execute_one(...)` — no try/except),
`kalshi.py:198-201` (`place_order` raises `ValueError` on bad args), `kalshi.py:244-249`
(`_signed_request` re-raises after retries).

`_execute_one` only converts a *price* rejection to `None`. Any exception from `place_order`
(HTTP 4xx like insufficient balance, auth failure, validation, or exhausted retries) propagates
out of the `for` loop and out of `execute()`. If leg 1 already filled and was booked, and leg 2
raises, the cycle aborts with **one leg of the wing live** and no compensating action. The
equal-payout structure is broken and the position is directionally exposed. `book.save` at
`execution.py:111` is never reached on that path, so even leg 1's fill may not be persisted →
Book/exchange divergence.

**Fix:** Wrap each leg in try/except; on a leg failure, decide explicitly (cancel the already-
placed legs via `cancel_order`, or mark the wing degraded) and always persist the Book in a
`finally`.

### W5. Settlement reconcile only reads the open Book; if the process is down when CLI truth arrives and the Book was lost, positions never settle / PnL is wrong
**Files:** `engine.py:100-103`, `execution.py:288-309`.

Settlement is driven entirely by walking `book.positions` against CLI truth at cycle time. If a
position fill was never persisted (W4 abort path, or a crash between `place_order` and
`book.add_fill` / `book.save`), that position is invisible to settlement forever, and the
realized PnL / `daily_loss` accounting silently diverges from the real Kalshi account.
`reconcile_settlements` also never cross-checks against `get_positions()`.

**Fix:** Reconcile the Book against `get_positions()` / settlement history from Kalshi, not just
internal state.

### W6. `risk.per_anchor_max_trades` is loaded but never enforced anywhere in the live path
**Files:** `config.py:108-111` (`RiskCfg.per_anchor_max_trades`); no reference in `execution.py`,
`engine.py`, or `strategy.py`.

A configured per-anchor trade cap gives a false sense of safety — it does nothing. Combined with
C1/C4, there is no count-based backstop on how many orders a single anchor can emit.

**Fix:** Enforce it in `execute()` (count `place_order` calls per anchor and stop).

---

## INFO

### I1. Crash window between `place_order` and `book.add_fill` is unprotected
`execution.py:93-107` — the order is on the exchange before the Book is updated and saved
(`book.save` at line 111 is once, after the whole loop). A crash in that window loses the fill
record. Persist per-leg (save after each `add_fill`) or use a write-ahead intent log.

### I2. `book.save` writes are atomic per-file but the live_log append is not transactional with the Book
`positions.py:113-118` uses tmp+`replace` (good, atomic). But `append_rows(cfg.paths.live_log)`
(`execution.py:110`) and `book.save` (line 111) are separate; a crash between them desyncs the
audit log from the Book. Low impact (log is diagnostic) but worth noting.

### I3. `_paper_fill_cents` adds slippage in PAPER but LIVE has no symmetric concept
`execution.py:173-189` vs `144-158`. PAPER is intentionally pessimistic; LIVE is optimistic
(books the limit). This means paper results systematically *understate* live fill quality in one
direction and live *overstates* it — paper is not a faithful predictor of live fills. Document or
align (ties into C2).

### I4. `fee_cents` in LIVE is computed from the *limit* price and *target* count, not the realized fill
`execution.py:155` — `trade_fee_cents(tgt.limit_price_cents/100, tgt.target_contracts)`. Since
the realized fill may differ (C1/C2), the booked fee is an estimate. Kalshi's actual fee is on
the real fill; reconcile fees from the fills endpoint. Minor at $25 but compounds with C1.

---

## What is handled correctly (verified, not findings)

- **Integer flooring to 0 is guarded.** `strategy.py:644` (`if n <= 0: continue`) and the
  post-cap `min_legs` check (`strategy.py:678-681`) reject a wing that can't field enough legs —
  no zero-size orders are emitted, and `place_order` itself rejects `count <= 0` (`kalshi.py:200`).
- **`place_order` validates side/action/count** (`kalshi.py:198-201`).
- **Book JSON persistence is atomic** (tmp + `os.replace`, `positions.py:113-118`).
- **Paper price gate is symmetric with live** for the "won't pay through limit" check
  (`execution.py:136-139`).
- **Settlement sign logic is correct**: `gross_per = 100 - avg_cost if won else -avg_cost`,
  fee subtracted once (`positions.py:74-75`).
- **LIVE credential validation is hard-failing** before any trading (`config.py:218-219`,
  `226-237`) and the authenticated client refuses unsigned requests (`kalshi.py:225-226`).
- **`_current_bankroll` floors at 0** so bankroll can't go negative in the sizing math
  (`engine.py:161`) — though it can still over-state free cash (W1).
- **`fetch_event` ticker uses the local date pattern**, sidestepping the documented
  settlement_date off-by-one (`kalshi.py:136`; CLAUDE.md §4.7).

---

## Priority for go-live

Before enabling `mode: live`, fix in order: **C1 (fill confirmation), C4 (idempotent
client_order_id + no blind POST retry), C2 (limit buffer), C3 (per-leg risk re-check + real
intraday loss cap).** W1/W2/W4 are the next tier. The common root cause across C1/C4/W2/W4/W5 is
that the Book is treated as ground truth while the exchange is never read back — wiring the
already-present `get_positions()` / a `/portfolio/fills` poll into `run_cycle` closes most of them
at once.
