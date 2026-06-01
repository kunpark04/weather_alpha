# Engine code review — adversarial read-only audit (2026-06-01)

**Scope:** every module under `weather_alpha/` (config, data, engine, execution, features,
fees, kalshi, live_fetchers, live_log, log, main, model, pmf, positions, report, scheduler,
strategy, tui, calibration, `__init__`, `__main__`), plus the operator scripts and configs
that gate the live money path. Read-only — no code modified. The multi-market refactor
(markets list, per-station settlement, per-city/account drawdown halts, the per-tz resident
scheduler, terminal reporter, Kalshi parser fix) was scrutinized alongside the older code.

## Verdict

**Counts:** 2 CRITICAL · 7 WARN · 8 INFO.

**Merge-readiness:** The core trading math (fees, equal-payout sizing, settlement win/loss,
PMF/bucket parsing, drawdown-halt sign/threshold/latch/reset, Kalshi RSA signing path,
balance/position fixed-point parsing) is **sound** — I verified each against the domain
invariants and with live read-only checks, and all 15 multi-market tests pass. **However,
two CRITICALs block unattended LIVE use:** (C1) the documented kill-switch / drawdown-halt
operator commands target the wrong state file for every real deploy config, so an emergency
halt silently no-ops; and (C2) the process-wide exposure cap that the refactor plan claims is
enforced is **not** enforced — N cities each size against the full balance with no cumulative
binding check. C1 is a one-line ops fix; C2 is bounded today only by flat-$2.50 sizing and the
(non-binding) daily-outlay cap. Fix both before adding a 2nd live city or raising stakes.

---

## CRITICAL

### C1. `kill.py` / `halt.py` default to a kill-switch/positions file no deployed bot reads — emergency halt silently no-ops
**Files:** `scripts/kill.py:39-41`, `scripts/halt.py:50-51`, all configs (`config/*.yaml`).
**NEW** (the path divergence widened with the per-process state dirs the refactor added).

`kill.py` and `halt.py` call `load_config(args.config)` with `args.config=None` when `--config`
is omitted, which resolves to the **default** `config/weather_alpha.yaml` →
`paths.kill_switch = data/KILL_SWITCH`. But **every deployable config points elsewhere** (verified):

| config | kill_switch | positions_snapshot |
|---|---|---|
| `weather_alpha.yaml` (default) | `data/KILL_SWITCH` | `data/positions.json` |
| `live.yaml` (the LIVE service runs this) | `data/live/KILL_SWITCH` | `data/live/positions.json` |
| `paper.yaml` | `data/paper/KILL_SWITCH` | `data/paper/positions.json` |
| `chicago_live.yaml` | `data/chicago/KILL_SWITCH` | `data/chicago/positions.json` |
| `houston_paper.yaml` | `data/houston/KILL_SWITCH` | `data/houston/positions.json` |

`deploy/weather-alpha-live.service:13` launches `--config config/live.yaml`, so the live bot
checks `data/live/KILL_SWITCH` (verified: `Path(cfg.paths.kill_switch).exists()` in
`execution.py:314`). An operator who runs the **documented** `python scripts/kill.py`
(`kill.py:9`) under pressure arms `data/KILL_SWITCH` — a file the live bot never stats — and
trading continues. The docstring's claim "the path is read from config … so it always matches
what the running engine checks" (`kill.py:14-16`) is **only true if the same `--config` is
passed**, which the usage examples omit. `halt.py` has the identical defect for the drawdown
reset/status, and it also reads/writes the wrong `positions.json` (so `--status` shows a stale
or empty Book).
**Why it's CRITICAL:** the kill switch is the last-resort money-loss stop on the live path; a
silent no-op defeats its entire purpose. **Fix direction:** make the live/paper deploy configs
the default these scripts load (e.g. require `--config`, or read `$WEATHER_ALPHA_CONFIG` which
the service could export), and/or have the scripts refuse to run if the target file's parent
dir doesn't match a known process dir. At minimum, fix the docstring usage to always pass
`--config config/live.yaml`.

### C2. Process-wide `total_exposure_max_pct` is asserted by the refactor but never enforced — N cities each deploy the full balance
**Files:** `weather_alpha/execution.py` (no exposure check anywhere), `weather_alpha/strategy.py:601,610`,
`tasks/multimarket_refactor_plan.md:76` (D4) and `:144` (Phase-3 claim). **NEW** (the false guarantee is new; the underlying gap is pre-existing).

D4 states *"`total_exposure_max_pct` and the daily-outlay cap already sum across all Book
positions … so they 'just work' process-wide with a shared Book."* The daily-outlay cap does
(see below); **`total_exposure_max_pct` does not.** Grep confirms it is referenced **only**
inside the strategy functions as a per-trade fraction-of-bankroll clamp
(`min(flat_stake_usd, total_exposure_max_pct * bankroll_usd)` at `strategy.py:601`;
`min(kelly…, total_exposure_max_pct)` at `:610`). `execute()` contains **no**
`book.exposure_cents()`-vs-cap check (verified: grep for `bankroll|balance|exposure` in
`execution.py` returns only logging lines). In `run_cycle` each market is sized with the
**same** `bankroll = _current_bankroll(cfg, book)` (engine.py:167) — the full balance — and the
balance is not decremented as earlier cities in the loop consume it (LIVE `bankroll_cents` is
fixed within a cycle; PAPER only drops by fees). So with K cities the cumulative open exposure
can reach K × 60% of the account with nothing binding it.
**Why it's CRITICAL on the live path:** the only cumulative gate that actually fires is
`Book.daily_outlay_cents` vs `risk.daily_max_loss_usd`, and on the $25 live account that cap is
**$100** (live.yaml:82) — 4× the balance — so it never binds for realistic city counts. Today
the real limiter is just *flat $2.50 × N cities* (6 × $2.50 = $15 < $25), which holds for one
city but is not a guarantee and silently breaks if `flat_usd`, the city count, or the cap
changes. There is **no check that Σ stakes ≤ available balance**, so a misconfiguration could
submit live orders summing past the cash balance.
**Pre-existing note:** `tasks/review_engine_logic.md:186-192` already flagged that the exposure
cap isn't enforced against the running Book; the refactor then *documented it as working*,
which is the new defect. **Fix direction:** enforce a real cumulative cap in `execute()` —
gate each leg on `book.exposure_cents() + delta_cost ≤ total_exposure_max_pct * bankroll`
(mirroring the existing daily-outlay breaker), and/or decrement the bankroll passed to each
market within the cycle. Separately, set `daily_max_loss_usd` to something meaningful on a $25
account.

---

## WARN

### W1. `Book.add_fill` mutates a *settled* position if the same `ticker:side` recurs
**File:** `weather_alpha/positions.py:56-77`. **PRE-EXISTING.**
`add_fill` keys by `ticker:side` and, if the key exists, blends into it **without checking
`p.settled`**. `execute()`'s delta logic treats a settled position as `current_qty=0`
(`execution.py:104-105`), so a new fill on a settled key would call `add_fill`, which then adds
contracts onto the *settled* Position — corrupting `contracts`/`avg_cost_cents` of an already-
realized row and leaving `realized_pnl_cents` stale. Today this is **latent**, not live,
because KXHIGHCHI tickers encode the event date (`KXHIGHCHI-26MAY30-…`) so the same ticker
never recurs across days. It becomes a real bug the moment a market reuses a ticker, or a
manual/odd re-fill lands on a settled key. **Fix direction:** in `add_fill`, if the existing
position is settled, open a fresh position (e.g. key by `ticker:side:anchor_date`, or refuse +
log).

### W2. LIVE fees are re-estimated, not read from the exchange → Book fee accounting drifts from reality
**File:** `weather_alpha/execution.py:266` (`trade_fee_cents(price_cents/100, filled)`),
`kalshi.py:_parse_positions` (no fee field surfaced). **PRE-EXISTING (acknowledged in HANDOFF AUDIT #8).**
On a confirmed LIVE fill the fee booked is the bot's *formula* fee, not Kalshi's actually-
charged fee. The two can differ (rounding, schedule nuances), so `Book.fees_paid_cents`,
realized PnL, and the PAPER-parity bankroll drift from the true account. The post-settlement
LIVE bankroll re-sync from `get_balance()` (engine.py:356) papers over the *balance*, but the
Book's per-position fee/PnL fields remain estimates. **Fix direction:** read the fill fee from
the order ack / fills endpoint and book that; extend `KalshiPosition` to carry
`fees_paid_dollars` (also needed for the read-only monitor — HANDOFF TUI row).

### W3. Deterministic `client_order_id` + partial-fill delta can leave the remainder unfillable (or silently un-placed)
**File:** `weather_alpha/execution.py:232` (`coid = f"wa-{anchor_date}-{tgt.ticker}-{tgt.side}"`), `:234-258`.
**PRE-EXISTING (C4 idempotency design).**
On a partial fill (e.g. 2/3), the Book records 2; next cycle `delta_qty = 1` and `place_order`
is called **with the same coid** but `count=1`. If Kalshi scopes idempotency by coid alone it
returns the original order (the new qty never places; the remainder fills only if the first
order is still resting). If it scopes by coid+payload it may place a *second* order. Either
branch is "safe-ish" but the behavior is exchange-dependent and unverified against a real
partial. The fill-confirmation delta (`filled = after_qty - before_qty`) is correct in
isolation, but combined with the fixed coid the partial-remainder path is fragile.
**Fix direction:** make the coid unique per attempt while keeping idempotency per *logical
intent* (e.g. include a cycle/attempt counter), or cancel-replace the resting order for the
true remaining qty. Add a mock-client partial-fill test.

### W4. Drawdown halt latches one cycle late, and within a multi-city cycle later cities trade on pre-settlement state
**File:** `weather_alpha/engine.py:119-128` (halt update at end of `run_cycle`), `:169-175`
(per-market `is_halted` gate at the start of each market). **NEW.**
The per-city/account halt is only **latched at the end of `run_cycle`**, after every market in
the loop has already decided + traded. So: (a) a settlement loss that crosses 25%/50% halts the
city/account only from the **next** cycle (≈ next day for that city), and (b) if city A's
settlement (early in the loop) trips the **account** 50% stop, cities B/C/… later in the *same*
cycle still trade because the latch hasn't been written yet. Given day+1 settlement and flat-$
sizing this is bounded, and the plan's "running balance" framing implies some lag, but it is
weaker than "halt all cities at 50%" reads. **Fix direction:** call `update_drawdown_halts`
*before* the market loop (using the balance carried into the cycle) in addition to after, and
re-check `is_halted` immediately before each `execute`.

### W5. `_check_daily_loss` cannot gate same-day risk (effectively dead for the current anchor)
**File:** `weather_alpha/execution.py:325-334`, `positions.py:93-95`. **PRE-EXISTING.**
`daily_loss_cents` only accrues at settlement (`settle()` adds `min(0, realized)` keyed by the
position's `anchor_date`). Settlement runs for **prior-day** positions, so the *current*
anchor date's `daily_loss_cents` is ~always 0 at decision time → the pre-cycle daily-loss gate
never trips for today. The intraday outlay breaker (C3) is the real same-day stop. This matches
the code's own C3 comment, so it's a documented design choice, but the daily-loss cap gives a
false sense of a same-day loss limit. **Fix direction:** none required if intentional; consider
deleting the misleading gate or renaming it "prior-day realized-loss cap."

### W6. `httpx.AsyncClient` is leaked on construction failure and in the TUI error path
**File:** `weather_alpha/kalshi.py:105-126` (client built before key load can raise), `tui.py:107-125`.
**PRE-EXISTING.**
`KalshiClient.__init__` opens `self._http = httpx.AsyncClient(...)` (line 105) *before*
loading the private key (line 123). If `_load_private_key` raises (bad/missing PEM), the
already-opened client is never closed (no `__aexit__` reached) → leaked socket/transport. In
the TUI, `on_mount` opens the client and enters it (`tui.py:110-111`) but `on_unmount`
(`:122-124`) only closes it if the app actually unmounts; an exception during mount after the
client is created can strand it. The headless path uses `async with` correctly.
**Fix direction:** build the client *after* credential validation, or wrap the key load in
try/except that closes the client before re-raising.

### W7. Per-leg `daily_outlay_cents` / `exposure_cents` disagree on whether to count settled rows
**File:** `weather_alpha/positions.py:98-107`. **NEW (drawdown/multi-market era).**
`exposure_cents()` filters `if not p.settled`; `daily_outlay_cents()` does **not** filter
settled. For the current daily-event flow no settled row shares today's `anchor_date`, so they
agree in practice — but the inconsistency is a latent foot-gun if settlement timing or ticker
reuse changes (a settled same-date row would inflate the intraday outlay and prematurely trip
the breaker). **Fix direction:** make `daily_outlay_cents` also filter `not p.settled` (worst-
case-loss-at-risk only applies to open stake).

---

## INFO

### I1. Kalshi ticker + event-guard use locale-dependent `%b`
`kalshi.py:142` and `engine.py:303` both build the date code via `strftime('%y%b%d').upper()`.
`%b` is locale-dependent; a non-English `LC_TIME` would produce a wrong month abbrev. Because
*both* the ticker and the guard use the identical expression they stay self-consistent — a bad
locale yields "no contracts returned" (silent no-trade), not a wrong trade. Linux deploy is
typically C/en_US, so low risk. Consider a hardcoded month-abbrev map for determinism.

### I2. Fee-aware gate's `max(contracts)*100` over-states the guaranteed win under non-equal-payout sizing
`strategy.py:672`. The gate uses the largest leg's payout minus *total* cost. For
`equal_payout` (production) all legs have equal contracts, so best-case == either-case (verified
numerically: 2 legs @ 47¢/41¢, flat $2.50 → +16¢ on either leg). For `prob_weighted` /
`market_weighted` the legs differ, so a smaller leg winning could net negative while the gate
passed. Production is equal_payout, so informational only.

### I3. `_contract_sort_key` falls back to 500 on any non-`°` subtitle
`kalshi.py:334-342`. A middle bucket whose `subtitle`/`yes_sub_title` lacks `°`, or a tail with
`strike_type=None`, sorts to the middle (500). Harmless — the sort is cosmetic and every
consumer (`run_wing_strategy`, `two_bucket_arb`) re-sorts by `bucket_lower_bound`.

### I4. `update_drawdown_halts` stops latching once balance ≤ 0
`positions.py:144-145` early-returns when `balance_cents <= 0`. At $0 the threshold is 0 so the
concept is moot (sizing emits nothing anyway), but a bled-to-zero account technically never
latches a new halt. Cosmetic.

### I5. Account-drawdown base mixes net-of-fee conventions
`engine.py:120-121` derives PAPER `balance_cents` from `_current_bankroll` (which subtracts
`fees_paid_cents`), while `update_drawdown_halts` measures drawdown against `realized_pnl_cents`
(already net of fees via `settle`). The threshold base and the drawdown are both ~net, so the
discrepancy is second-order. Informational.

### I6. `reset_halt(station)` does not reset the account HWM
`positions.py:122-134`. Resetting one city clears that city's HWM but leaves `account_hwm_cents`
stale, so a subsequent `update_drawdown_halts` could re-latch the **account** halt off the old
peak. Arguably intended (one city's reset shouldn't clear the account peak), but worth a doc
note.

### I7. `_resync_bankroll_after_settlement` skipped when net realized is exactly 0
`engine.py:117` gates the LIVE re-sync on `if total_realized:`. A cycle whose settlements net to
exactly 0¢ (offsetting win/loss) skips the balance re-query. Vanishingly rare and self-heals on
the next nonzero settlement.

### I8. `features.py` dynamic-`exec` of notebook cells is an integrity/`exec` surface
`features.py:130-135` compiles and `exec`s arbitrary `§3.*` code cells from
`notebooks/model_v3.ipynb` into a seeded namespace. This is intentional ("zero drift by
construction") and only runs on the **model-enabled** path (not the live model-free path), but
it means model-path behavior depends on untracked notebook content and runs unsandboxed.
HANDOFF already lists porting `§3` to a module as the long-term fix.

---

## Areas explicitly verified SOUND (no finding)

- **Kalshi fee** `ceil(0.07·N·P·(1−P))` (`fees.py:16-22`) — correct, ceil-once-per-leg, charged
  at entry; `net_ev_cents` subtracts it.
- **Equal-payout sizing** `stake_i = K·ask_i`, `K = stake/Σask`, `n_i ≈ K` (`strategy.py:617-628`)
  — verified numerically; flat-$2.50 → ~3 contracts/leg as documented.
- **Settlement win/loss + PnL** (`positions.py:79-96`, `execution.py:410-438`) — YES/NO win
  determination correct, `gross_per = 100 − avg_cost` on win / `−avg_cost` on loss, fees
  subtracted, daily-loss accrual sign correct. Per-station filter prevents cross-city
  contamination (verified by `test_per_station_settlement_no_contamination` and by hand: KMDW
  90-91 wins at 91°F, KHOU 92-93 loses at the same 91°F).
- **Bucket parsing / PMF integration** (`pmf.py`) — `<=K`/`<K`/`>=K`/`>K`/`lo-hi` parse
  consistently; `bucket_upper_bound('<71')=70`, `('<=71')=71`; subtitle and strike-fallback
  paths agree (`less` cap 72 → `<=71`, `greater` floor 79 → `>=80`).
- **Kalshi RSA-PSS signing path** (`kalshi.py:86-94,114,230`) — signs the FULL path including
  `/trade-api/v2`; verified httpx joins an absolute-rooted request path onto the base_url
  prefix so the signed string == the wire path exactly.
- **Balance / position fixed-point parsing** (`kalshi.py:269-305`) — `balance_dollars` (str or
  num) → cents, `position_fp` signed → side+contracts, `market_exposure_dollars` → avg price;
  legacy-field fallback works; zero positions filtered.
- **Drawdown halt sign/threshold/latch/reset** (`positions.py:136-159`) — peak-tracking,
  `(peak−current) ≥ pct·balance`, per-city 25% / account 50%, latched, HWM-reset-on-clear all
  verified incl. the first-trade-loss seed edge ( −$9 on $25 trips city@25%, not account@50% ).
- **Kill-switch gating + settlement-still-runs** (`execution.py:71,114-121,317-322`,
  `engine.py:190-198`) — pre-cycle hard-abort, per-leg re-check, and the documented
  KillSwitchTripped→settlement-still-runs fix all behave (covered by `test_run_cycle_honors_halt`).
- **State persistence round-trips** (`positions.py:174-216`, `scheduler.py`) — Book JSON
  (incl. all drawdown fields) and both schedulers round-trip; atomic tmp-replace writes.
- **Per-tz scheduler** (`scheduler.py:160-237`) — fires each city at its own local 1 PM across
  tz incl. DST (verified by tests at 17/18/20 UTC summer and 18 UTC winter).
- **Mode isolation** — every deploy config points live/paper at *distinct* `positions_snapshot`
  files, so a single Book never mixes modes (the per-process invariant holds via config).

---

## Resolution (2026-06-01)

All findings from this report **and** a follow-up deep-review pass were fixed + verified — 28
multi-market tests pass, the paper smoke runs both cities clean (exit 0), and credless kill/halt +
the authenticated read path are both confirmed. The TUI active-driver finding is the one **deferred**
item (tracked in HANDOFF `TUI`).

**This report (first pass):**
- **C1** kill/halt wrong-file → require `--config`; `Config.config_path` stamped into HALTED/reset lines.
- **C2** exposure cap enforced in `execute()` (Σ exposure + leg ≤ pct×balance); `daily_max_loss_usd` 100→15.
- **W1** `add_fill` opens a fresh position rather than mutate a settled one; **W4** drawdown latches before the
  loop + after each market; **W5** same-day daily-loss gate relabelled; **W6** httpx client built after cred load;
  **W7** `daily_outlay_cents` excludes settled rows.
- **W2/W3** documented, not changed (formula fee reconciled by the balance re-sync; stable `coid` kept = safe
  under-fill over double-order risk) — both need a live fill to change safely.
- **I1** locale-safe `event_date_code`; **I3** sort falls back to strike; **I2/I4/I5/I6/I7** clarifying comments;
  **I8** left (model-path notebook `exec`, already tracked).

**Follow-up deep review (same day):**
- **#1** kill/halt load with `require_live_creds=False` (emergency stop works from a credless shell).
- **#2** resident loop records only markets that returned a result — an errored (isolated) city retries next tick.
- **#3** LIVE deltas size off exchange truth (`get_positions`), not the local Book.
- **#4** an unfilled LIVE order is cancelled (no late unbooked fill).
- **#5** paper bankroll no longer double-counts settled fees.
- **#6** top-ups book the marginal fill price, not the blended post-order average.
- **#7** `event_ticker` sourced from the API response (the event-date guard is no longer tautological).
- **#8** `MarketAnchorScheduler` fires only within 60 min of the anchor (no off-anchor catch-up on a late restart).
- **#10** `per_anchor_max_trades` counts order attempts, not just booked fills.
