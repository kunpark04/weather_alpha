# Multi-market refactor plan — "one live bot for all live cities" (+ one paper bot)

_Draft 2026-05-31. Target architecture: segregate by MODE at the process level
(one LIVE process, one PAPER process), trade MULTIPLE cities WITHIN each process._

---

## 1. Goal

Replace the single-market engine with a multi-market one, so that **one process trades a
*list* of cities**, while keeping **live and paper in separate processes** (the safety we
care about — a paper fill must never touch real-money Book/bankroll/PnL). Concretely:

- **One LIVE bot** iterating all live cities (one real account, one Book, one bankroll).
- **One PAPER bot** iterating all paper cities (simulated, no creds).

Mode stays a **process-level** property → no in-process live/paper mixing, no Book
segregation problem (that was the footgun in a single-process-mixed design).

---

## 2. The hard constraint: per-city 1 PM anchors across timezones

A one-shot process fires at **one wall-clock instant**. Cities in different tz have
different 1 PM anchors (NYC 1 PM EDT = 17:00 UTC; Chicago = 18:00 UTC; LA = 20:00 UTC).
So a single one-shot process can only serve cities that **share a 1 PM** — i.e. the same tz.

**Resolution — two sub-options:**

- **A. One process per (mode, timezone)** *(recommended first step)*. "One live bot per tz":
  Chicago + Houston (both Central) → one Central LIVE process firing at 13:00 CT; NYC
  (Eastern) → a separate Eastern LIVE process at 13:00 ET; etc. Fits today's one-shot
  systemd-timer deploy. Live cities cluster by tz anyway (Chicago/Houston/Dallas/OKC/NOLA/
  SATX are all Central → one Central bot covers them).
- **B. One long-lived `--loop` process across all tz** *(true single process)*. A resident
  process whose internal scheduler fires each city at its *own* local 1 PM. Achieves
  literally "one live bot for all live cities regardless of tz," but needs the `--loop`
  path + per-market scheduler state + an always-on process. More work, resident footprint.

**Recommendation (updated 2026-05-31 per user): build the multi-city CORE + ship Option B — ONE bot.**
Three user steers all point to B: (i) cities TBD but **prepared for all US timezones**,
(ii) the user wants **one** bot, not one-per-tz, (iii) **per-city** drawdown limits.
Key realization: **the multi-city core (markets list, shared Book/bankroll, per-city +
account risk caps, per-station settlement, back-compat) is identical for A and B** — only
the *scheduling* differs. B reuses the **existing `--headless --loop` minute-tick scheduler**,
extended to (a) iterate the markets list and (b) fire each market at its *own* local 1 PM
(per-market anchor). So **one resident process** trades every US-tz city at the right local
time, and — because all state lives in one place — the per-city drawdown stops are trivial
to enforce (one view of every city's P&L + the shared balance). **Option A** (per-tz
one-shots → *several* bots) stays documented as a lean fallback, but is no longer the target.

**Cost of B:** one process running across the daily trading window (not a quick alarm),
holding the RW key in memory for that window, with `systemd Restart=on-failure`. The
scheduler + state-persistence (`scheduler_state.json`) **already exist** — B just extends
them to multi-market + per-market-anchor.

**All-US-tz requirements (bake in now):** each market carries its exact **IANA tz name**
(`America/New_York`, `America/Chicago`, `America/Denver`, `America/Los_Angeles`, the
**no-DST** zones `America/Phoenix` + `Pacific/Honolulu`, and `America/Anchorage`). DST is
then automatic via the tz-aware scheduler. Never hard-code a UTC offset.

> ⚠️ tz handling: under **B** (target), **one config lists every US-tz city** and the
> scheduler fires each at its own local 1 PM — no same-tz restriction. Under **A** (fallback),
> one config = one timer = one tz, so the loader would require all markets in a config to
> share `local_tz` (else "split into one config per tz").

---

## 3. Design decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | **Config gains a `markets:` list**, each `{name, event_pattern, station, local_tz}`. `mode` stays top-level (process = one mode). Strategy/risk/kalshi blocks shared. | Each process = one mode, N cities. Per-market only needs market/station/tz; the wing knobs are shared (allow per-market overrides later). |
| D2 | **One shared `Book` + one `bankroll_cents` per process.** Positions key by `ticker:side` (ticker already encodes the city → no collision). `verify_bankroll` adopts the account balance once. | A live account has ONE balance; trading N live cities draws from the same pot. Shared Book = the account's full position set. |
| D3 | **Mode is process-level** → `execute(... client if cfg.is_live() else None)` is unchanged; no per-market mode routing. | Segregating by process means no live/paper mixing inside one Book — eliminates the contamination risk. |
| D4 | **Risk caps become process-wide** where they gate the account: `total_exposure_max_pct` and the daily-outlay cap already sum across all Book positions (per date / via `exposure_cents()`), so they "just work" process-wide with a shared Book. `per_anchor_max_trades` stays **per-market** (runaway backstop per event). `per_contract_max_pct` unchanged. | Protect the *account* across cities; cap each event's leg count locally. |
| D5 | **Settlement becomes per-station.** Each Position carries its `station` (or derive via a ticker-prefix→market map); `settle_if_due`/`reconcile_settlements` group pending positions by station and fetch each station's CLI. `fetch_live_cli` already generalizes (verified for KHOU). | Chicago settles on KMDW, Houston on KHOU, etc. — one CLI per station. |
| D6 | **Kill switch + state are per-process** (one `data/<process>/` dir, one `KILL_SWITCH`). | One halt per bot; isolated state per process (live vs paper, and per-tz group). |
| D7 | **Back-compat:** if a config has no `markets:` list, synthesize a 1-element list from the legacy `station`/`local_tz`/`execution.market_event_pattern`. | The existing single-city configs (`chicago_live.yaml`, `houston_paper.yaml`) keep working untouched. |

---

## 3a. Per-city drawdown stop (risk detail)

Per decision #3, the **primary** live risk gate is a per-city drawdown circuit-breaker, not
just an account-wide loss cap.

- **Rule:** halt a city (place no new trades) when **that city's drawdown ≥ 25% of the
  *current* account balance**. "Drawdown" = peak-to-trough of the city's cumulative realized
  P&L (track a per-city high-water mark). The 25% is of the *running* balance (live
  `get_balance` / Book bankroll), so the dollar trigger scales with the account (≈ $6.25 on
  $25; grows as it compounds, tightens if it bleeds).
- **Whole-account stop — 50%:** halt *all* cities if the **whole account** draws down
  **50% from its high-water mark**. (Looser than per-city by design: one city trips at 25%,
  but the whole book only stops at a 50% total bleed.)
- **State:** per-city HWM + realized P&L persist in the Book (survive restarts); a halted
  city stays halted until manually reset (a per-city analog of the kill switch).
- **Supersedes:** replaces the unrealistic `daily_max_loss_usd: 100` (on a $25 account) as
  the live gate; the per-leg + total-exposure caps still apply underneath.

**Confirmed 2026-05-31:** per-city stop at **25%** + whole-account stop at **50%**; both
measured from the **peak** (high-water mark); both **latched until the user manually resets**.
On any trip, print a concise terminal line (§3b).

**Sizing note (flat $, unchanged):** bet *size* is a fixed **$2.50/trade** (equal-payout
`K = round(2.50 / sum_asks)`) — this refactor does **not** change sizing. The drawdown stops
are an independent risk layer: they decide *whether* a city may trade, never *how much*.

---

## 3b. Terminal reporter (decision + action messages — confirmed 2026-05-31)

Beyond the debug log, the bot prints **one concise human-readable line per event** to the
terminal (stdout) — so watching the process (or `journalctl -f`) shows exactly what it
decided and did. Required events:

- **1 PM decision, per market:**
  - `[13:00 CHI] ✅ ENTER — YES 72-73 @ 47¢, YES 74-75 @ 41¢  (sum 0.88, stake $2.49 +$0.04 fee)`
  - `[13:00 CHI] ⏭️ SKIP — sum_asks 0.93 > 0.90 cap`  (always give the reason: cap / no contracts / city halted / already-positioned)
- **Fill result:** `[13:00 CHI] FILLED 2/2 — outlay $2.49, fee $0.04`  ·  partial → `FILLED 1/2 — 74-75 leg unfilled (ask moved)`
- **Settlement:** `[CHI 30MAY] 💰 SETTLED — high 91°F, bucket 90-91 won → +$3.10 net`
- **Risk halts (§3a):** `[CHI] ⛔ HALTED — drawdown $6.30 ≥ 25% of $25.10; reset to resume`  ·  `[ACCOUNT] ⛔ HALTED — total drawdown 50% (peak $25.10 → $12.55); all cities stopped`

One line per event, prices in ¢, money in $, no multi-line dumps. Implemented as a small
`report(msg)` helper (timestamped, city-tagged) called from the decision / execute / settle /
risk-gate paths; mirrors to the structured logger but is its own concise stream.

---

## 4. Implementation phases + files

**Phase 1 — Config schema** (`config.py`)
- Add `MarketCfg{name, event_pattern, station, local_tz}`; `Config.markets: list[MarketCfg]`.
- Loader: parse `markets:` if present; else synthesize a 1-element list (back-compat).
- Validation: non-empty; **all markets share `local_tz`** (Option A); live config still requires creds.

**Phase 2 — Engine loop** (`engine.py`)
- `run_cycle` iterates `cfg.markets`: per market → `fetch_event(market.event_pattern)` →
  `_tradeable_contracts` → `_dispatch_strategy` → `execute` (shared Book, process mode).
- `preflight` logs per-market; `verify_bankroll` once (process bankroll).
- `CycleResult` becomes per-market (list) or aggregated.

**Phase 3 — Risk gates** (`execution.py`)
- Confirm `total_exposure_max_pct` + daily-outlay checks read the **shared Book totals**
  (already do — they sum across tickers/dates), so they gate the *process*, not per-city.
- Keep `per_anchor_max_trades` per-market (fills counter resets per `execute` call).
- Ensure sequential per-market `execute` accumulates exposure in the shared Book so the
  cumulative cap binds across cities within a cycle.

**Phase 4 — Settlement** (`engine.py` / `positions.py`)
- Add `station` to `Position` (or a `ticker→market` map from `cfg.markets`).
- `settle_if_due`/`reconcile_settlements`: group pending positions by station, fetch each
  station's CLI once, reconcile.

**Phase 5 — main / deploy** (`main.py`, `deploy/`)
- `main` loads a multi-market config; one Book; `run_cycle` loops markets.
- Deploy: one timer per (mode, tz) process — e.g. `weather-alpha-central-live.{service,timer}`
  (Chicago + any Central live cities), `weather-alpha-central-paper.{service,timer}`
  (Houston + Central paper cities), `…-eastern-…` for NYC, etc. Update `deploy/README`.
- Configs: `central_live.yaml` (`markets:` [chicago, …]), `central_paper.yaml` ([houston, …]), …

**Phase 6 — Tests**
- Multi-market `run_cycle` with a mock client returning distinct events per pattern.
- Shared Book across cities (no ticker collision); process-wide exposure/daily-loss caps bind.
- Per-station settlement (mock CLI per station).
- Back-compat: a single-market (legacy) config still loads + runs.
- Isolated-temp-path harness (like the #5 kill-switch fix verification).

**Phase 7 — Rollout (do NOT touch live until tested)**
1. Build + unit-test against mocks.
2. Run the multi-market **PAPER** process first (Houston + a 2nd paper city) to validate
   the loop / settlement / risk in paper for a week.
3. Only then migrate live cities into the multi-market LIVE process.
4. Keep the single-city instances as a fallback (back-compat) during cutover.

---

## 5. Risks / footguns

1. **Multi-tz in one one-shot process** (§2) — *the* big one. Enforce same-tz-per-config; multi-tz = separate processes (or Option B).
2. **Shared live bankroll** — sizing + the exposure cap must gate the *cumulative* account deployment across cities, not per-city. Verify the cap binds on the running Book total.
3. **Don't reintroduce live/paper mixing** — mode stays process-level. Never put a paper market in the live process's Book.
4. **Per-station settlement** must use each position's own station (not a single global one).
5. **Live money path** — refactor is on the trading path; gate behind paper validation + tests before any live use.
6. **Risk-cap semantics change** (process-wide) is deliberate, account-level protection — document it; don't let N cities each deploy the full account.

---

## 6. Decisions (locked 2026-05-31)

1. **A vs B → B (ONE bot).** Build the shared core + **Option B**: one resident `--loop` process that trades every US-tz city at its own local 1 PM (reuses the existing minute-tick scheduler, made multi-market + per-market-anchor). Option A (several one-shot bots, one per tz) is the documented fallback only. (§2)
2. **Per-market overrides:** **shared knobs** for v1; add per-city overrides only when a city proves it needs them.
3. **Risk caps → drawdown stops (confirmed).** Per-city halt at **25%** of the running account; whole-account halt at **50%** of the running total; both measured from the **peak**, both **latched until you manually reset**. Sizing is unchanged — **flat $2.50/trade** (orthogonal to these stops). See §3a.
4. **Which cities go live:** **TBD — design for *all* US timezones.** Hard-code nothing; the markets list carries each city's IANA tz (incl. no-DST AZ/HI). Live vs paper = which list a city is in.
5. **Terminal reporter (confirmed).** One concise line per event on the terminal — each market's 1 PM decision (`ENTER — YES 72-73 @ 47¢ …` / `SKIP — reason`), fills, settlements, and risk halts. See §3b.

---

## 7. Effort

~1–2 focused days: config (~½ day), engine loop + risk (~½ day), per-station settlement
(~½ day), tests + paper rollout (~½–1 day). The bulk of the *risk* is in (a) the multi-tz
decision and (b) the shared-bankroll cap semantics — both resolved above.
