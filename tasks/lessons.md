# Lessons

Self-improvement log. After any user correction, capture the pattern + a rule that
prevents recurrence. Most recent first.

---

## L21 — Never call a dir "empty"/redundant from a partial filter; enumerate ALL of it before a destructive op

**Context.** Verifying the relocated droplet's local pull dirs, I counted only `*.zip` in
`weather-alpha\data\orderbook`, got 0, reported it an "empty leftover", and proposed deleting it — the
user said "yes" on that basis. The dir actually held **88.5 MB of raw `.jsonl`** depth (KXHIGHCHI,
6/1–6/6). The delete didn't fire only because I'd gated it on a full recursive `-Force` enumeration
(`if count==0`), not the zip count; that guard surfaced the contradiction, I re-checked (the days were
all redundant with the external zip store), re-surfaced to the user, and got fresh confirmation before
removing.

**Rule.** Don't characterize a directory/dataset as empty / complete / redundant from a **filtered**
view (`-Filter *.zip`, one glob, one extension). Before any destructive action enumerate **all** contents
recursively incl. hidden (`Get-ChildItem -Recurse -Force`), check total size, and confirm the data exists
elsewhere. Pair the destructive call with a guard that **fails closed** (delete only inside an
`if (count==0)` branch, not unconditionally). If a prior claim proves wrong, STOP — a "yes" given on a
false premise ("an empty dir") does not authorize deleting real bytes; re-surface and re-confirm.

**Why it matters.** Counting one extension and calling the rest "nothing" is exactly how live data gets
deleted under a stale authorization. A full enumeration costs milliseconds; a wrong "empty" costs
unrecoverable bytes. Sibling of the "look at the target before deleting" rule.

---

## L20 — The droplet carries a sparse + partial checkout, NEVER the full repo

**Context.** After flattening the droplet (user `fa` → `weather-alpha`, home = project; the project was
later relocated into a nested `~/weather-alpha` `0700` subdir on 2026-06-08), the user set a
standing rule: *"Only pull what is necessary for the logger in droplet (never pull/clone entire repo)."*
The `rsync` rebuild carried only fa's existing **sparse-checkout** (cone: `weather_alpha config scripts
deploy`) of a **partial clone** (`--filter=blob:none`, `.git` = 1.3M) — no `notebooks/`, `historical/`,
`archive/`, `docs/`, or model artifacts. The 897M home is `.venv` (564M) + live `data` (193M), not code.

**Rule.** Any code on the droplet is a **partial (`--filter=blob:none`) + sparse-checkout** of the
**runtime-only** paths (`weather_alpha`, `config`, `scripts`; `deploy` optional, for unit reinstalls) —
never `git clone`/`git pull` of the whole tree (which would drag the notebooks, 3.4-yr deep-history study,
archive, and model artifacts the **model-free** bot never loads). The droplet has **no GitHub key** today,
so it can't fetch at all — the safest default for this rule; if pull capability is ever needed, wire a
sparse + `--filter=blob:none` fetch of the named paths, not a clone. Code-side sibling of the lean-data
rule [[L19]].

**Why it matters.** A full clone on a 24G/1GB box wastes disk + bandwidth and pulls research/data the
production path never touches; it also widens the exposure surface on a box that holds the live RW key.
Lean checkout = faster, cheaper, smaller blast radius, consistent with the per-user write boundary the
migration established.

---

## L19 — Relocate exactly what was asked; don't bundle adjacent data the user didn't name (scope-creep on infra ops)

**Context.** Asked to move the **raw weather market data** out to `..\data\weather` "with the logger data",
I relocated BOTH the orderbook (raw market data) AND the **bot logs** (`data/{paper,live}` live_log /
positions) + repointed the tracker + set `WA_LOCAL_DATA`/`WA_DATA_DIR`. The user corrected: "keep the bot
logs here in this project folder. I just want the raw weather market data moved." I reverted the bot-state
move + the tracker change; only the orderbook moved (and later the truth tape, when explicitly asked).

**Rule.** On a relocate / move / cleanup op, move ONLY the named target. "Move X" ≠ "move X plus the
adjacent Y that the same pipeline happens to touch." Bot logs and market data are different categories; the
user named one. When one instruction could touch several datasets, relocate the explicit one and leave (or
ask about) the rest — don't infer that "consolidate" means "everything." This is CLAUDE.md rule #9 (match
scope to ask) applied to infra/data ops; the mirror of [[L9]] (test the *whole* proposal) — here, do
*exactly* the proposal, not more.

**Why it matters.** Over-moving bot logs out of the repo silently broke the tracker's default path (I had to
add, then revert, a `WA_DATA_DIR` knob) and would have scattered state the user wanted version-adjacent. A
relocate that grabs more than asked is a reversible-but-noisy detour that erodes trust on "just do X."

---

## L18 — Filter a multi-entity log by entity before per-entity analysis; and INVESTIGATE an anomalous validation number, don't rationalize it

**Context.** Built `scripts/forward_edge_tracker.py` to score forward Chicago fires from the resident
bot's `live_log.parquet`. The resident PAPER bot logs **all 20 cities** to one log, but `_fires()`
filtered by mode + date only — **not by series** — so it lumped every city's filled legs into each
"fire" and matched them against Chicago's settled winner. Result when truth finally landed: 0% win
rate, breakeven WR **290%**, edge **−290¢/$payout** (impossible — a coverage bet can't lose >100¢ per
$1 payout), and a Chicago-only realized of **−$19.99** that contradicted the bot's whole-book −$4.99.
The fix was one line: `df[df.ticker.str.startswith(f"{series}-")]`. Truth after the fix: Chicago has
fired **0** times forward (paper all `no_target`, live gated) — the "4 fires" I'd reported were
entirely other cities.

**The worse mistake: I had already SEEN the bug and explained it away.** The earlier smoke test
(`--since 2026-05-01`) printed "1 fire, −$222.81, 12,499 contracts" and I called it "the pre-production
test fills, exactly the stale data I'm validating against" — a rationalization. A 12,499-contract,
−$222 "fire" on a flat-$2.50 strategy was nonsensical and was the **same multi-city bug**; I shipped
the script and updated docs on the back of a validation run whose output I had hand-waved.

**Rules.**
- **Any per-entity metric computed from a shared multi-entity log/table must filter to that entity
  FIRST.** When a file aggregates many keys (cities, accounts, symbols), the entity filter is part of
  correctness, not a nicety — state the key and assert it (here: ticker prefix == series).
- **An impossible number is a bug signal, not a data quirk — stop and trace it.** Breakeven WR >100%,
  edge worse than the max possible loss, a per-slice total exceeding the whole — these are structurally
  impossible, so the code is wrong. Never explain an impossible value as "weird input."
- **Don't certify a tool on a validation run you had to rationalize.** If the smoke-test output needs a
  hand-wave ("that's just test data"), the smoke test FAILED — investigate until the number is
  explained by the data, or the tool isn't validated. (Echoes [[L10]]/[[L14]]: bound/trust numbers
  against what's structurally possible; here, a sane-range check would have caught it immediately.)
- **Validate a multi-entity reader on ≥2 entities, like a parser ([[L5]]).** One-city output can look
  plausible; the cross-entity contamination only shows when a second entity's rows are present.

**Why it matters.** I reported "4 Chicago forward fires, −$4.99" to the user as fact; the truth was
N=0 for Chicago. A confidently-wrong forward number is exactly what would corrupt the
accumulate-to-significance decision the tracker exists to support. The impossible 290% breakeven was a
free tell I'd have caught by sanity-checking the range before reporting.

---

## L17 — "Survives fees" ≠ "confirmed edge": replay the EXACT config net of the REAL fee in DOLLARS, then discount a max-selected, single-regime OOS for multiplicity + regime before calling it real

**Context.** Asked whether the production edge survives the real Kalshi fee, I replayed the exact
production config (`scripts/chicago_oos_netfee.py`) with the real `ceil()` fee — the §1.6 `+5.0¢`
Chicago headline (`per_city_synthesis.py`) had used a **continuous fee proxy** (`0.07·a·(1−a)`) in
**normalized ¢/$payout**. The fee question had a clean answer (edge survives: realistic OOS
`+5.0 → +4.6¢`, ~8% drag), but the result was THREE optimisms stacked, only one of which was the fee.

**The three stacked optimisms (each a different way to over-claim an edge).**
1. **Continuous fee < real ceil fee.** The ceil rounds each per-leg fee UP; at ~2–3 contracts/leg that
   is ~0.4–0.5¢/$payout the proxy omitted. Cost the strategy with the SAME fee fn the live path calls
   (`weather_alpha.fees.trade_fee_cents`), never a smooth approximation.
2. **Normalized ¢/$payout hides the dollar magnitude.** +4.6¢/$payout is a healthy *normalized* edge
   but only **+$16/yr** on flat $2.50. Report BOTH the normalized edge (comparability) AND the actual
   dollars (the "is this worth it" decision) — a normalized number alone launders lunch money as a
   strong edge.
3. **A max-selected, single-regime OOS is not a confirmed OOS.** Chicago is the WINNER of 20 cities
   scored on the same tape (§1.6) → its OOS look is multiplicity-inflated; and its OOS window is
   *entirely* the recent dense-liquidity regime (realistic fill 22→33→98→100% by year), so realistic≈
   proxy OOS holds only because adverse selection recently vanished — through-cycle edge is ~+2.5¢, not
   +5¢. Honest verdict: "positive, borderline (binomial p=0.11, bootstrap P(≤0)≈0.05), recent-regime,"
   not "confirmed."

**Rules.**
- **To kill or confirm an after-cost edge, replay the EXACT production fn with the EXACT live fee, in
  dollars** — not a normalized proxy. State the dollar magnitude alongside the normalized one.
- **Run the structurally-correct significance test, not just a t-stat.** For a high-WR coverage
  (favorite-longshot) bet the test is WR vs the per-day breakeven (binomial / Clopper–Pearson),
  reported next to the dollar t-test and a bootstrap CI. "Positive point estimate" ≠ "significant."
- **Discount a selected estimate for HOW it was selected.** If the city/param was the max of N on the
  same data, the OOS p is multiplicity-inflated; if the OOS window is one regime (liquidity, season),
  the edge may be regime-bound, not forward. Name both discounts explicitly.
- **Answer the asked question AND surface the one that actually binds.** The fee question was a clean
  YES; the binding uncertainty was significance + selection + regime. Don't let a tidy answer to the
  asked question imply the decision is settled.
- **Check the headline's own caveat before quoting it.** `per_city_synthesis`'s docstring already said
  the continuous fee made its edges "a touch optimistic" — the proxy flagged its own optimism; read it
  before quoting +5.0¢ as if it were the live-realizable number.

**Why it matters.** "+5.0¢ realistic OOS, only Chicago durable" reads as a confirmed, ship-it edge; the
honest version ("survives fees, but +$16/yr, p≈0.11, selection- and regime-contaminated → needs genuine
forward data") drives the opposite call: don't scale, don't fund, accumulate forward fills first.
Extends [[L12]] (OOS-first; bigger sample is still in-sample), [[L14]] (separate backtest OUTPUT from
input skepticism — here the inputs were the fee model + the selection), and [[L10]] (bound the noise
before reporting a delta).

---

## L16 — Harden EVERY external-call path, not just the obvious one; and a doc that says "spaced/retried" must point at code that does it (recurrence of L4)

**Context.** The user asked whether the live/paper bots had traded and whether there was an
error. PAPER had traded (26 legs); LIVE had not (Chicago-only, every day's wing failed the
`max_ask_sum < 0.90` affordability gate — correct, not a bug). The real error: on 2026-06-06 the
PAPER bot logged 7 `❌ cycle failed` lines. The traceback (`logs/paper/weather_alpha.log`) was
**`httpx.HTTPStatusError: 429 Too Many Requests`** on `GET /markets?event_ticker=…` inside
`kalshi.fetch_event` — Kalshi rate-limited the **read** path when every same-tz city fired its
`fetch_event` in the same second at the top of the 1 PM anchor window.

**Two mistakes it exposed.**
1. **Asymmetric hardening.** Commit `1bcf1f7` added 429/throttle retry+backoff to the
   *order-placement* path only. `fetch_event` (the public read every cycle makes for every city)
   had **no** retry and **no** 429 handling — so the most-frequent external call was the least
   protected. Recovery relied entirely on the coarse ~5-min cycle retry.
2. **Doc claimed a guarantee the code didn't enforce (L4 again).** HANDOFF §1.6 said trade cycles
   "iterate markets sequentially (spaced) so they don't 429" and that the preflight burst was "the
   only residual." Neither was true: `run_cycle`'s loop had **no** inter-market delay, and the
   trade cycle itself 429'd. The "higher auth tier" was assumed sufficient; it wasn't (the
   orderbook logger shares the droplet IP).

**Fix.** Added `scheduler.inter_market_stagger_seconds` (default 0.0; 0.5 in the resident
`live.yaml`/`paper.yaml`); `run_cycle` AND `preflight` sleep it between markets. **The stagger alone
proved insufficient** on deploy: `fetch_event` is *unauthenticated* (`self._http.get`, not signed), so
the read-only-auth "higher tier" never applied to it — the read shares Kalshi's low per-IP tier with
the always-on orderbook logger, and **15/20 preflight reads still 429'd at 0.5 s**. So a Retry-After-aware
retry/backoff was added to the public read (`KalshiClient._get_with_retry`): 429 → wait → retry → success.
It took **two iterations measured on the live droplet** — 4 attempts cut 15/20 → 2/20, then 6 attempts
(~9.5 s window) reached **0/20** (verified). Stagger + retry together = clean restart logs and no city
lost to a throttled anchor read. HANDOFF corrected to match each step (don't let it drift again).

**Rules.**
- When you add resilience (retry/backoff/rate-limit handling) to one external call, **audit the
  sibling calls** — especially read paths that run far more often than the write path you just
  fixed. Resilience asymmetry is a latent outage.
- A burst is per-**process+IP**, not per-call: count *every* concurrent caller (here: bot fan-out
  + the always-on logger on the same host) before declaring a rate-limit headroom "enough."
- **Verify a documented guarantee against the code that enforces it** before trusting it (L4). If
  HANDOFF says "spaced"/"retried"/"isolated", grep for the sleep/retry/try — don't assume.
- Read the actual traceback (`logs/<mode>/weather_alpha.log`), not just the operator one-liner
  (`❌ cycle failed (see logs)`), before judging severity. The one-liner hid a benign-but-real 429.

---

## L15 — A maker (passive) fill is adversely selected — "post at the bid" is not free spread; and read the data you HAVE before deciding you must wait for new data

**Problem.** The realistic-fill work showed the limit-at-ask *taker* spread eats the edge for 19/20
cities, so the obvious hope was MAKER execution (rest a bid, save the spread). I nearly framed it as
"wait weeks for the order-book logger, then test." Two errors: (a) I hadn't checked whether existing
data could answer it, and (b) "posting at the bid saves the spread" silently assumes the fill is
unbiased. Verifying the backfill schema *directly* (the user's "do not assume code, verify it") found
`taker_side` per trade (`yes`=lifted ask, `no`=hit bid) — enough to simulate a resting yes-bid on 3.4
YEARS of real trades now (fills iff a `taker_side=="no"` sell prints at <= our bid in the window). The
result killed the hope: maker fills are **adversely selected** — the WINNING leg fills LESS than the
losing leg in 14/20 cities (Chicago 32% vs 42%; New Orleans 42% vs 74%). The market gives you the
passive fill exactly when the bucket is turning into a loser, so you collect losers and miss payoffs;
for Chicago (the one real edge) maker is strictly worse than taker (-11.8c vs +1.2c in-sample).

**Solution / rules.**
- **Before concluding "we must wait for new data," read the schema of the data you already have.**
  One field (`taker_side`) turned a multi-week wait into a one-run answer on years of history.
- **A fill you only get passively is selected, not random.** Never model a maker fill as "same win
  rate, minus the spread." Split the fill rate by outcome (winner vs loser); if winners fill less,
  the 'saved' spread is repaid as missed payoffs. State the selection direction.
- **Make the optimism explicit and check robustness to it.** The trade-tape fill model ignores
  queue/size -> it's an UPPER bound on fills; real maker fills are lower and MORE adverse. A negative
  verdict under an optimistic model is conservative.

**Why it matters.** Execution tricks are the classic "free money" trap, and tying the test to a
multi-week wait would have delayed a decision-grade verdict the existing tape delivered immediately.
Complements [[L14]] (separate backtest OUTPUT from input skepticism) and [[L11]] (a limit inferred
from how WE collected data, not from what the source holds).

---

## L14 — Don't call a backtest-POSITIVE result "unprofitable"; say "not live-realizable" and name the input you distrust

**Problem.** I showed the wing's `edge` (= coverage - cost - fee = EV per $1) RISING as the anchor
moved into the afternoon, then called the late anchors "unprofitable / not tradeable." The user
caught the contradiction: edge and PnL are the SAME quantity (backtest PnL is proportional to edge),
so a rising edge is a rising backtest PnL -- the late anchors are MORE profitable in the backtest,
not less. My "unprofitable" conflated *"the backtest says +X"* with *"I don't believe the backtest"*.
The real claim was that the late edge is NOT LIVE-REALIZABLE, for input reasons -- which a Chicago
diagnostic then made concrete: the trade the +1c ask is set off goes from **5 min stale at 1 PM to
41 min stale at 4 PM** (the market goes quiet -> a "4 PM fill" is fictional), coverage drifts up
(partial answer-leakage), and the late in-sample edge does not survive OOS (**2 PM: IS +8.1c ->
OOS -5.8c**). So the late number is a stale-price artifact, not negative PnL.

**Solution / rules.**
- **Keep "what the backtest reports" separate from "what I believe is real."** When you distrust a
  backtest-positive result, say "the backtest shows +X but it is not realizable because INPUT is
  unrealistic (here: a stale/optimistic ask proxy)", never "it is unprofitable." The latter reads as
  a data finding when it is a judgment override.
- **A metric is only as honest as its inputs.** `edge = coverage - cost - fee` is the right EV
  concept, but `cost = last_trade + 1c` is a proxy. Before trusting an edge number, check the price
  input's realism -- staleness (minutes since the last trade), liquidity, spread. The proxy is least
  biased on a fresh/liquid book (midday) and increasingly fictional late.
- **State the optimism direction explicitly.** ALL these edge numbers are mildly optimistic (proxy
  ask); the bias is smallest where the book is fresh (1 PM, ~5-min trades). The real fix is
  order-book asks (logged forward by the orderbook logger; absent historically).
- **A rising in-sample curve is not a free lunch** -- OOS-test it (here 2 PM flipped negative) AND
  sanity-check the input that is moving (here cost falling on ever-staler prices, not coverage).

**Why it matters.** Calling a backtest-positive result "unprofitable" erodes trust in every other
number I report and hides the real, fixable issue (the price input). Complements [[L12]]/[[L13]]
(OOS, axis power) -- here, separate the model's OUTPUT from skepticism about its INPUTS, and name the
input.

---

## L13 — A cross-correlation is only as powered as its WEAKER axis; don't headline a null (or an effect) when one axis is known-thin

**Problem.** I correlated an 11-year, exact-degF NOAA forecastability axis against a **67-day** Kalshi
coverage axis, got Spearman -0.12, and headlined "the NOAA test REFUTES the -0.75 forecastability
finding." The weather axis was robust; the *coverage* axis was one thin season (11-45 fired days/city,
Chicago compressed to 96%). After the historical backfill made coverage a 3.4-year axis (Chicago 80%,
543 fired), the SAME NOAA proxies re-correlated at **-0.56** (spread) / -0.53 (de-seasonalized) -- the
null flipped to a moderate effect purely because the second axis gained power. I had flagged the
coverage axis as thin, yet still led with "refuted," which I then had to retract.

**Solution / rules.**
- **Match the power of BOTH axes before interpreting a cross-city/cross-unit correlation.** A robust
  measure correlated against a noisy one inherits the noise; the result is dominated by the weaker
  axis. n=19 cities with one axis estimated from ~67 days is not a test of anything yet.
- **If one axis is known-underpowered, REPORT THE CORRELATION AS UNRESOLVED, not as a null or an
  effect.** "Can't tell until coverage is robust" was the correct headline; "-0.75 refuted" was not.
  A null from a thin axis is indistinguishable from attenuation-by-noise.
- **When an estimate is sensitive to a fixable axis, fix the axis before concluding.** The backfill
  that powered the coverage axis was already in flight; I should have waited for it rather than
  publish the thin-axis null as the headline.
- **Corroboration across INDEPENDENT methods is the real signal.** The deep-coverage result is
  trustworthy because two independent forecastability measures (Kalshi modal error AND NOAA exact
  weather) both land at -0.5 to -0.7 -- not because either single correlation is large.

**Why it matters.** I reversed a substantive root-cause conclusion ("weather doesn't explain
coverage" -> "weather does explain coverage") inside one session, purely on axis power. Headlining the
thin-axis null misled for a full turn. Extends [[L12]] (a bigger/again-robust sample changes the
answer) and project rule #4 (sample-size discipline) to the *correlation* setting: discipline applies
to every axis, not just the one you're focused on.

---

## L12 — On NEW/expanded data, re-apply the OOS time-split BEFORE reporting any edge; in-sample-only on a bigger sample is still in-sample

**Problem.** After the historical backfill turned 67 days into 3.4 years, I ran the full per-city
decomposition + PnL backtest and reported the in-sample headline (+$254 total, "10 profitable
cities", Chicago +$141) — *in-sample only*, on data the strategy + gate (0.90, win_prob 0.92) were
chosen on. The user had to interject "make sure THIS TIME we have an in-sample and OOS 80:20 split."
When I built the temporal split, the story deflated sharply: the rich per-city edges (Miami +17.4c,
NYC +9.8c in-sample) **decayed to ~breakeven OOS**; only **Chicago** kept a positive OOS edge
(+8c/trade, +$12/75 fired days); IS->OOS edge rank-correlation was only +0.15-0.24; the in-sample
selected basket was ~breakeven OOS (-$3). The one robustly-generalizing result was the *direction*
(drop_lower_ask >> drop_higher_ask placebo, OOS, everywhere) — not the exploitable level.

**Solution / rules.**
- **A bigger sample is not a held-out sample.** Expanding the data (more days, more history) does
  NOT substitute for a train/test split. The FIRST result reported on any new/expanded backtest
  must be the OOS split, not the full-sample fit — especially when params/gate/strategy were chosen
  on overlapping history. (Project rule #4 already mandates time-split validation; apply it
  reflexively, not on request.)
- **Default split: temporal, per-series 80:20** (first 80% of each city's days = IS/decide, last
  20% = OOS/validate). Report IS and OOS side by side; flag thin-OOS rows (here OOS fired < 15) as
  anecdote, and judge generalization on the deep series only.
- **Separate "direction generalizes" from "level generalizes."** A signal can beat its placebo OOS
  (mechanism real) while its after-cost edge collapses to breakeven (not tradeable). State both;
  don't let a strong placebo contrast launder a thin absolute edge.
- **Expect in-sample optimism and pre-empt it.** When a gate self-selects cheap days and the
  strategy was picked post-hoc, the in-sample edge is an upper bound; quote the OOS number as the
  headline and the in-sample as context, never the reverse.

**Why it matters.** The in-sample "+$254 / 10 cities" would have justified expanding live trading to
more cities; the OOS truth ("Chicago marginally, everyone else ~0") justifies the opposite. Reporting
the in-sample number first risks anchoring a real-money decision on optimism. Complements [[L10]]
(bound the noise) and project rule #4 (OOS-style validation) — here, do it *first* and *unprompted*.

---

## L11 — A data "cap" inferred from what's been collected (or from one endpoint tier) is not a real cap; check the provider's documented retention/historical tier before asserting a limit

**Problem.** Asked whether the ~67-day backtest window could be extended, I asserted a hard limit
*twice*, both wrong. First: the markets "launched ~March 2026, so 67 days is the entire history" —
but that was only when **our** `backfill_cities.py` started collecting. The user pushed back
("Kalshi temperature markets are open year round… NO reason it should be bounded to 67 days").
Probing the API: `KXHIGHCHI` settled **events** go back to **2021-08** (1,746 days, a continuous
wall — 28-31/month for 58 months). I then over-corrected into a *second* wrong claim — that the
**prices** layer is "windowed to ~67 days" — because the *live* `/markets` + `/candlesticks`
endpoints do stop at the cutoff (402 settled markets, oldest 2026-03-30; legacy `HIGHCHI` returns
0). But Kalshi documents a **live-vs-historical data partition**: a separate **`/historical/*`**
family (no auth) serves every market/trade/candlestick back to inception. `GET /historical/cutoff`
= 2026-04-05 boundary; `GET /historical/markets?series_ticker=KXHIGHCHI` returns **8,575 markets to
2021-08-20**, and the *oldest* day (`HIGHCHI-21AUG19-T81`) still has **11 candlesticks + 105 trades
(all pre-1 PM)**. True depth ≈ **4.8 years**, not 67 days — a ~26× error that had silently framed
every "within-noise, can't tell if the edge is real" conclusion this session.

**Solution / rules.**
- **A limit inferred from on-disk data measures OUR collection window, not availability.** Before
  "that's all there is," query the provider for the full extent.
- **One endpoint returning a short window ≠ the data is gone.** Check whether the provider
  partitions live vs historical (Kalshi: `GET /historical/cutoff` + `/historical/*`). Probe **each
  layer** — events, markets, trades, candlesticks can each have different reach (here events were
  deep, *live* prices shallow, *historical* prices deep).
- **Read the data-access docs before claiming a retention limit.** The historical tier was one
  documented endpoint family away the entire time; one web search surfaced it.
- **When the user insists a limit is wrong, treat it as a strong prior to re-derive from primary
  sources, not to defend.** The user was right twice; I was wrong twice.

**Why it matters.** A phantom data cap bounded the whole research program — "67 days, within noise"
framed dozens of turns and nearly closed the book on the edge as unprovable. The real 4.8-yr archive
makes Chicago/NY backtests ~20× larger and potentially *powered* enough to confirm or kill the edge.
Extends project rule #4 (sample-size discipline): before concluding "underpowered," confirm you have
actually pulled all the available sample.

---

## L10 — Re-implemented gates must match production's EXACT boundary; bound the noise before reporting a PnL delta

**Problem.** An adversarial review of the walk-forward suite found two faults I had shipped as
findings: (1) **boundary mismatch** — production fires iff `sum_asks < max_ask_sum` (rejects `>=`,
`strategy.py:586`), but my walk-forward rebuilt the wing with a loose gate then re-applied
`cost <= 0.90` (inclusive), counting 108 exactly-0.90 wings the live bot never trades; this inflated
the headline (S +$9.15→+$2.81, SW +$5.91→+$0.54, H0 +$11.09→+$6.23, test days 398→381). (2)
**within-noise point estimates reported as findings** — S−B +$31, full-wing edge +0.105, "H0 is
best" were all presented as results when every per-trade edge was statistically indistinguishable
from zero at one season (n≈110–338), and the full-wing edge was a day-population selection artifact
(matched-day: the 2-leg actually wins).

**Solution / rules.**
- **When a backtest re-applies a production gate outside the production function, copy the exact
  predicate** (operator *and* strictness) or call the production function directly; then count how
  many cells sit on the boundary. A `<=` where production uses `<` silently books trades the live
  engine can't. (Same family as [[L4]]: code must match the stated contract.)
- **Before reporting a PnL delta as a finding, bound the noise** — MDE, a t/sign test, and a
  leave-one-cluster(city)-out. State estimates with their band; a delta inside it is
  "indistinguishable from zero," not an effect.
- **To attribute an edge to an instrument/gate, hold the day-population FIXED** (matched sample).
  A headline on a self-selected subpopulation measures the *selector*, not the instrument.
- **Independent adversarial review earns its cost on any result that will drive a decision** — I
  would otherwise have shipped inflated, within-noise conclusions as validated edge.

**Why it matters.** Confident in-sample point estimates inside the noise band masquerade as
validated edge and drive real-money decisions; a one-tick boundary mismatch compounds it by
crediting fills that can't happen live. Complements [[L9]] (test the whole proposal) — here, test it
*correctly* and *report it honestly*.

---

## L9 — When validating a user's proposed strategy, test the COMPLETE proposal, not a subset

**Problem.** The user proposed a hybrid: trade the 2-leg wing when it has edge, **else fall back to
the full 3-leg wing**, monitored intraday. I built the walk-forward with only the **2-leg** wing in
every variant and concluded "monitoring doesn't help / the proposal doesn't beat status quo." The
user caught it — I had never tested the full-wing fallback *at all*, which is the core instrument
for the no-edge cities. When added, the full wing was the **highest-edge signal** (+0.105/trade,
94% coverage) and the 1 PM hybrid was the **best variant** (+$33 vs status quo). My negative verdict
had been drawn from a strategy missing half of what was proposed.

**Solution / rules.**
- **Enumerate every instrument/branch the user specified BEFORE coding the test**, and confirm each
  appears in the implementation. A proposal of the form "A, else B" must exercise **B**, not just A.
  Restate the proposal back as a checklist (here: {2-wing edge path, full-wing fallback, intraday
  window}) and verify coverage — I had built only {2-wing, window}.
- **A negative result on a partial implementation is not a verdict on the proposal.** Scope the
  claim to what actually ran ("tested the 2-wing only"), never "the idea fails."
- This is project CLAUDE.md behavioral rule #9 ("match scope to ask") applied to **test design**,
  and the complement of [[L5]] (test the real/second *case*): also test the *whole* specified case.

**Why it matters.** A confidently-wrong negative verdict can bury a user's correct idea — the full
wing was genuinely the best signal here, and I'd have discarded it. A partial test that masquerades
as a complete one is worse than no test: it ends inquiry with false authority.

---

## L8 — Gitignore the secrets DIRECTORY, not just file extensions

**Problem.** The repo `.gitignore` had `.env`, `*.key`, `*.pem` — which protected the PEM, but on
the live VM `secrets/kalshi-rw.env` (not literally `.env`) and `secrets/readwrite-key-id` (no
extension at all) were **NOT ignored** and showed as stage-able in `git status`. Extension-based
rules silently miss credential files with non-standard names. The security audit caught it; no
secret had actually been committed (the PEM stayed ignored, repo private), but a stray
`git add -A` on the VM would have committed the key id.

**Solution / rules.**
- **Ignore the whole secrets directory** (`secrets/`) plus `*.env`, not just `*.pem`/`*.key`/`.env`.
  A directory rule catches every credential file regardless of name/extension (`readwrite-key-id`,
  `kalshi-rw.env`, future additions).
- **Verify with `git check-ignore` on the actual secret paths** — not just "we have a .gitignore" —
  and on **every host that holds secrets** (the deploy box, not only dev). This gap existed *only*
  on the VM; the local tree happened to never have those files.
- **Scan full history, all branches** for leakage (`git rev-list --all` + `git grep 'PRIVATE KEY'`),
  not just the current tree — a committed secret persists in history even after deletion.

**Why it matters.** Extension-based secret ignores give false confidence; the one credential file
without a matching extension (an API key id) is exactly the one that slips into a commit.

---

## L7 — CRLF breaks bash piped from PowerShell; run remote bash single-line or strip CRs

**Problem.** Going live, I piped PowerShell here-strings (`@'...'@`) to remote `ssh … bash -s`.
The here-strings carry Windows **CRLF**, and PowerShell also appends a trailing newline to the
piped stream, so remote bash saw `\r`: it broke a `case` ("syntax error near unexpected token
`newline`"), appended `\r` to a path (`check_kalshi_auth.py\r` → "No such file"), and corrupted
the env file's value. Two failed iterations before I spotted the carriage returns.

**Solution / rules.**
- **Default to a single-line remote command:** `ssh host "cmd; cmd; cmd"` — no here-string, no
  multi-line, no CRLF to leak. Reliable. (Use single-quoted PowerShell wrapping so `$(...)`/`$VAR`
  evaluate on the remote, not locally.)
- **If a multi-line script is unavoidable, don't pipe a Windows here-string** — strip CRs
  (`$s.Replace("`r","")`) *and* expect a trailing newline on the pipe, or `scp` a real LF file and
  run that. Piping `@'...'@` raw will always leak `\r`.
- **A `\r` in the error is the tell.** "syntax error near unexpected token `newline`", or a path
  printed with a trailing char that shouldn't be there, = CRLF — fix the endings, don't chase the
  apparent auth/path/syntax bug.
- Same line-ending family as L6 (single-line commands for the *user* to paste), different
  direction (me → remote bash). When in doubt about line endings, go single-line.

**Why it matters.** Like L6, the surface error points everywhere except the real cause (line
endings), burning iterations on phantom auth/path bugs — costly mid-go-live.

---

## L6 — Hand the user single-line commands to paste; `\`-continuations break on paste

**Problem.** Twice this session a multi-line shell command I gave (a `\`-continued `git clone`,
then a `\`-continued `printf … >> authorized_keys`) failed when the user pasted it: the
backslash-newline arrived as backslash-**space**, so bash read `\ ` as an escaped literal space
and mangled the command — the clone got a bogus URL (` git@github.com…` → wrong SSH user →
"Permission denied"), and the printf detached from its `>>` redirect (key printed to screen,
never written). Both *looked* like auth/file failures but were pure paste-mangling.

**Solution / rules.**
- **A command for the user to paste must be ONE physical line** — no `\` continuations. A single
  long line pastes intact; a continued one frequently splits at the backslash.
- **Make it fail safe if it does split.** Avoid `… && rm` / `… >> file` tails that, detached,
  would execute or truncate something. For appending a key, a single `echo 'KEY' >> file` beats a
  multi-clause `printf … && chmod`.
- **Always pair a fragile write with a verify step** (`tail -2 authorized_keys`, `ssh -T`) so a
  silent no-op surfaces immediately, not two steps later.

**Why it matters.** A mangled-on-paste command throws a *misleading* error (auth/repo, not
syntax) that sends debugging down the wrong path — exactly what happened twice before we spotted
the backslash.

---

## L5 — Verify on REAL data/runtime, not just mocks; unit tests miss the parse-level crash

**Problem.** The 28 multi-market unit tests all passed, yet the first PAPER smoke run against
the *live* market crashed on Houston (`KXHIGHTHOU`): its bucket range lives in `yes_sub_title`
with `between`/`less`/`greater` strikes, not Chicago's `subtitle` — a shape no mock exercised.
Separately, the pre-live review found the kill switch wrote/read the *wrong file* because the
ops tool didn't take the running bot's `--config` — invisible to any single-process test.

**Solution / rules.**
- **Run the real thing once before declaring done.** Mocks encode *your* assumptions about the
  payload; a live fetch (or a real authenticated read) surfaces the field that's actually
  null/shaped-differently. A green unit suite is necessary, not sufficient.
- **Multi-instance / multi-market behavior needs a multi-instance check.** Anything keyed by
  config path, station, or tz (kill-switch file, positions snapshot, per-city state) can't be
  validated by a single in-process test — exercise ≥2 configs.
- **Parse the second example, not just the canonical one.** When generalizing a parser, feed it
  a genuinely different member of the set (Houston, not another Chicago) before trusting it.

**Why it matters.** "Tests pass" hid a crash that would have taken the live city down on day one;
the wrong-file kill switch would have failed exactly when it was needed most.

---

## L4 — Don't document a guarantee the code doesn't enforce

**Problem.** The refactor plan asserted the exposure cap "just works," but `execute()` never
actually checked `total_exposure_max_pct` — the cap existed only as a config field and a
sentence in a doc. The review caught it; the enforcement had to be *added* to match the claim.

**Solution / rules.**
- **A documented invariant must point at the line that enforces it.** Before writing "X is
  capped / X is enforced / X can't happen," grep for the check and confirm it runs on the live
  path. If there's no enforcing line, either add it or downgrade the doc to "intended, not yet
  enforced."
- **Config knob ≠ enforcement.** A field in `config.py` is an input, not a guarantee; the guard
  that reads it is the guarantee. Same trap as a CLI flag that's parsed but never used.
- Pairs with CLAUDE.md §4.5 ("don't hide filters") — the inverse failure: don't *advertise* a
  filter that isn't there.

**Why it matters.** A false safety claim is worse than a stated gap: the reader sizes up trusting
a cap that would never have fired.

---

## L3 — A fix can expose an adjacent bug; re-trace the whole path after changing it

**Problem.** Making `kill.py`/`halt.py` require `--config` (the correct fix for the wrong-file
bug, L5) then routed those ops tools to the *live* config — which tripped the live-credential
check in `load_config`, so the emergency-stop tools refused to run from a credless ops shell.
The first fix surfaced a second, opposite failure (now fixed via `require_live_creds=False`).

**Solution / rules.**
- **After a fix, walk the *new* code path end-to-end** — especially shared helpers the change now
  reaches (here, `load_config`'s cred gate). A fix that changes *which* inputs/paths are used can
  activate a guard that was dormant before.
- **Emergency / read-only tools must not depend on write-mode preconditions.** A kill switch or a
  status reader has to work in the most degraded shell (no creds, no network) — load with the
  strictest checks OFF.

**Why it matters.** The whole point of the kill switch is to work when things are bad; a fix that
makes it require live creds defeats it in exactly the credless incident-response scenario.

---

## L2 — Verify the actual failure path in code before calling something a "blocker"

**Problem.** Explaining a lean-server deployment risk, I asserted the headless `--loop`
would crash because **`refresh_data` (HRRR/herbie) raises**. Wrong: `fetch_live_hrrr`'s
`ImportError` is caught by `fetch_all_live`'s per-source try/except, so a missing `herbie`
is logged and skipped — no crash. The real hard-fail was **`load_bundle`**, which insists
all 5 weather parquets exist and raises `FileNotFoundError` on the absent HRRR file. I only
located it correctly after the user pushed back ("I'm confused, rephrase") and I read `data.py`.

**Solution / rules.**
- **Read the function before naming it the culprit.** When claiming "X crashes / X is the
  blocker," open X *and the layer around it* (its callers' try/except) first — a caught
  exception is not a crash.
- **Trace the failure to the exact line**, not the plausible-sounding one. The hard-fail was
  one layer away from where I pointed (`load_bundle`, not `refresh_data`).
- Same root as L1's "don't act on a phantom problem": verify, then assert.

**Why it matters.** A confidently-wrong mechanism sends the user (and the next agent) toward
the wrong fix — here, "install/trim herbie" instead of the real one, "don't make `load_bundle`
require model-only parquets in model-free."

---

## L1 — Don't fire large parallel tool batches; one failure cancels them all

**Problem.** I repeatedly sent ~10–25 tool calls in a single message. The harness
rule is: if *one* call in a parallel batch fails, every other call in that batch is
**cancelled**. So a single bad call produced a wall of `Cancelled: parallel tool
call …` results, wasted the whole batch, and looked alarming. This happened twice in
a row — and the second time was *immediately after* the user told me to stop.

Two triggers that set off the failing call:
1. **Fabricated identifier.** I cited a commit hash (`9b3a7e8`) that did not exist in
   the repo → `git` exited 128 → cancelled the batch.
2. **Read-before-exists.** I assumed `tasks/lessons.md` existed and queued ~15
   Read/Grep/Bash calls against it. It didn't exist → first call failed → batch
   cancelled.

**Solution / rules.**
- **Small batches.** Only parallelize calls that are genuinely independent AND each
  highly likely to succeed. When unsure, send **one call at a time**.
- **Probe before fan-out.** Verify a file exists (single `Glob` or `ls`) before
  queueing multiple reads/edits against it.
- **Never invent identifiers.** Commit hashes, paths, tickers — read them from
  `git log` / the filesystem first. Don't reconstruct from memory.
- **Don't act on a phantom problem.** Before "fixing" something (e.g. a
  "corrupted" config), confirm it's actually broken. In the prior turn every check
  showed the config was fine, yet I still rewrote it. Verify first, mutate second.

**Why it matters.** Cascading cancellations waste context, obscure the one real
error under ten fake ones, and erode trust — especially when repeated after a
correction.

---
