# Weather Alpha — Handoff (multi-market Kalshi daily-high; seed: KMDW / KXHIGHCHI)

_Last updated: 2026-06-01_

Probabilistic ML model + **multi-market** production trading engine for Kalshi daily-high
markets — a configurable `markets:` list of US cities, each fired at its own local 1 PM
(seed market: **KXHIGHCHI** at **KMDW**, Chicago Midway). The production strategy is
**model-free** `market_wing` (market-anchored); the v3 model (calibrated PMF over integer °F
at T = 1 PM, mapped to Kalshi's 6-contract bucket structure) is retained for the
model-enabled path only. **LIVE since 2026-06-02** (Chicago) + a PAPER shadow (mode is per-process;
the LIVE order/fill path is armed but the first real fill is still unexercised until the next
1 PM CT anchor — see §7 `L2`). **Deployed:** a LIVE bot (CHI) + a PAPER bot (CHI+HOU) + orderbook
logger run 24/7 on a DigitalOcean droplet; a daily local pull collects
the orderbook zips — see §7 `DEPLOY`.

---

## 0. Current state at a glance

> ### PRODUCTION STRATEGY (paper) — pivoted 2026-05-29
> **`market_wing` + `drop_lower_ask`, flat-$ sizing — MODEL-FREE.** Anchors on the
> market modal (the v3 model is *not* used — see §1.5), keeps the higher-ask adjacent,
> sizes a flat **$2.50/trade** on the real **$25** Kalshi account. Now wired into the
> engine (config dispatch, replacing the old `joint_kelly` default) and validated
> end-to-end in paper on live data. Backtest @ $25 (flat-$2.50, `max_ask_sum 0.90`,
> fee-aware): **24 fires, 96% WR, +$7.98 (+32%), −7% max DD**.
>
> _Prior candidate (Kelly, model-anchored):_ `wing + drop_lower_ask` — +$308 / 91% /
> Sharpe 2.85 / −12.5% on $1000. Higher Sharpe, but flat sizing brings market_wing's
> drawdown to ~parity while keeping more fires, and market_wing needs no model →
> far simpler live path. Sizing rationale + caveat: memory `project_sizing_flat_dollar`.

> ### PRODUCTION MODEL
> **`model_v3.ipynb`** — T = 1 PM anchor, 46 features (incl. HRRR path-5).
> Artifacts at `data/model_v3_artifacts/`. CRPS 1.204 °F; Top-1 bucket 45.8 %;
> Top-3 (±1 bucket) 87.8 % over 1,966 OOF days (2021-01-01 → 2026-05-21).

> ### MULTI-CITY SCOPE + EXECUTION (2026-06-05)
> Deep backfill unlocked (Kalshi `/historical/*` tier → **3.4 yr, 20 cities**, ~1,250 days/city for
> the deep ones). Per-city OOS synthesis: **only Chicago has a durable edge** — every other city's
> realistic (spread-eaten) edge fails OOS or decayed as the market matured. **Maker execution does
> NOT rescue it:** posting at the bid fills (38–89%) but is **adversely selected** (winning legs fill
> less than losing legs in 14/20 cities), so it can't capture the spread — for Chicago maker is
> strictly worse than taking. PAPER now shadows **all 20** cities, the logger logs **all 20** series
> (both read-only-authenticated), and all 20 settlement stations are **verified vs Kalshi's own
> rules**. Details: **§1.6**. **Forward-confirmed (paper, 2026-06-09):** the 20-city wing ran
> net **−$9.42** and latched its 50% account halt — the bleed is entirely the 19 non-Chicago
> cities (Chicago never fired) — the live forward-OOS confirmation of "only Chicago"; **§1.8**.

| Item | Value |
|---|---|
| Best strategy | `wing` + `drop_lower_ask` (agreement-required) |
| Win rate | 91 % (20/2 day-level over 22 fires) |
| Net PnL | +$308 on $1000 starting bankroll |
| Sharpe (annualized) | +2.85 |
| Max drawdown | -$181 (-18%) |
| Best alternative | `market_wing` + `drop_lower_ask` (+$371 on 40 days, but Sharpe +1.66 / max DD -$369) |
| Best model | model_v3 (1 PM anchor) |
| Model CRPS | 1.204 °F |
| Top-1 / Top-3 bucket | 45.8 % / 87.8 % (2°F bins, 5-yr OOF) |
| Kalshi-resolution Top-3 on agreement days | **100 %** (61-day window) |
| OOF window | 2021-01-01 → 2026-05-21 (1,966 days) |
| Kalshi history | 2026-03-21 → 2026-05-26 (67 days) |

---

## 1. Strategy verdict

### 1.1 Top-level strategies tested (8 dispatched via `--strategy <name>`)

| Strategy | Verdict |
|---|---|
| `joint_kelly` (default per-bucket Kelly) | ❌ -$795 / 12% WR — model has no per-bucket edge vs market (Brier 0.69 vs 0.43; LOO α = 0) |
| `two_bucket_arb` | ❌ same root cause as joint_kelly |
| `wing` (3-leg agreement-required) | ✅ baseline +$40 / 60% WR; **+drop_lower_ask** is the production winner |
| `wing_any` (no agreement) | ❌ -$149 indiscriminately; agreement filter is load-bearing |
| `market_wing` (market-anchor wing) | ⚠ profitable with `drop_lower_ask` (+$371) but 2× the drawdown of `wing` |
| `variance` | ❌ -$366 (1/9 W/L) — dispersion signal is wrong direction |
| `hrrr_bias` | ❌ -$457 (0/9 — never wins) |
| `regime_confident` | ❌ no profitable parameter exists in [peak_p × hrrr_gap] grid |
| ~~`hard_floor`~~ | dropped per current direction |

### 1.2 Wing variant matrix (parameter sweeps within `--strategy wing`)

| Variant | Days | W/L | WR% | PnL | Sharpe | Max DD |
|---|---|---|---|---|---|---|
| **`wing` + `drop_lower_ask`** (PRODUCTION) | 22 | 20/2 | **91%** | **+$308** | **+2.85** | **-$181** |
| `wing` + `drop_worst_leg` | 11 | 8/3 | 73% | +$278 | +1.31 | -$445 |
| `wing` prob-weighted | 10 | 6/4 | 60% | +$156 | +1.57 | -$162 |
| `wing` market-weighted (pow=5) | 8 | 6/2 | 75% | +$84 | +0.71 | -$168 |
| `wing` eq-payout (loose, max=1.00) | 10 | 6/4 | 60% | +$40 | +3.87 | -$2 |
| `wing` eq-payout (strict, max=0.97) | 7 | 6/1 | 86% | +$39 | +4.37 | $0 |
| `wing` + `drop_higher_ask` (PLACEBO) | 26 | 17/9 | 65% | -$431 | -1.55 | -$919 |
| `market_wing` + `drop_lower_ask` | 40 | 35/5 | 88% | +$371 | +1.66 | -$369 |
| `wing_any` + `drop_lower_ask` | 41 | 32/9 | 78% | +$330 | +0.64 | -$1090 |

### 1.3 Why `wing + drop_lower_ask` works

- **Wing structure**: cover model's modal Kalshi bucket + both positional adjacents → ±1 bucket coverage.
- **Agreement filter** (`require_agreement=True`): only fire when model_modal == market_modal. On agreement days (34/61), **Top-3 hit rate is 100%** at Kalshi-bucket resolution.
- **`drop_lower_ask` transform**: keep modal + the higher-ask of the two adjacents. The market is empirically 82% accurate at picking which adjacent to bet on (confirmed by placebo: `drop_higher_ask` loses $431 in the opposite direction).
- **Equal-payout sizing**: `stake_i = K × ask_i` so payout on any winning leg = K dollars regardless of which won.

### 1.4 Midnight anchor (v4 model) — tested 2026-05-28, NOT viable

Tested whether the wing edge survives at a **midnight forecast anchor** (model
`v4`, midnight market snapshot) instead of 1 PM. It does not — on either axis.

**Model side (v4 midnight OOF):** far weaker than v3 at bucket prediction.

| Metric | v3 (1 PM) | v4 (midnight) |
|---|---|---|
| §10.2 fixed-2°F Top-1 / Top-3 (5-yr OOF) | 45.8% / 87.8% | 19.1% / 48.7% |
| Kalshi-resolution model Top-1 / Top-3 | 44.3% / 91.8% | 15.2% / 27.3% |
| Agreement with market modal | ~56% | ~15–18% |

Even at **equal information** (both at midnight), the market beats v4 decisively
(market Top-3 90.9% vs v4 27.3%). The market's edge is an intraday-information
effect: its Top-1 sharpens 40.9% (midnight) → 62–64% (1 PM) and its 2-leg
coverage 69.7% → 95.5%. At midnight the book is still diffuse.

**Strategy side:** both top-2 strategies flip to losing on the midnight anchor
(identical params; only model→v4, anchor→0):

| Strategy | 1 PM (v3) | midnight (v4) |
|---|---|---|
| `wing + drop_lower_ask` | +$308 (20/2) | **−$125** (7/2, 9 fires) |
| `market_wing + drop_lower_ask` | +$371 (35/5) | **−$388** (43/20) |

`wing` fires rarely (9 days — v4 rarely agrees with the market) and over-bets
(it carries `assumed_win_prob=0.99`, true at 1 PM where the wing settles 94–100%
but false at midnight where it settles ~83%). `market_wing` fires often but the
diffuse midnight book means a 2-leg wing covers only ~70% at ~the same cost →
68% WR, −71% max DD. **Conclusion: the edge lives in the 1 PM information
environment; the midnight anchor is not tradeable.** Repro scripts in §6.

**Mechanism (quantified, `scripts/v3_v4_why.py`):** v4's PMF is ~2.4× wider than
v3's (CRPS 2.85 vs 1.20 °F; 80% CI 14.3 vs 5.9 °F; peak prob 14.8% vs 31.2%)
because a midnight anchor has (a) no same-day observations — by 1 PM the daily
high is already realized on **58% of days** — (b) no 12Z HRRR forecast (not yet
published at midnight; v4 has zero HRRR features), and (c) no running-max hard
floor. The gap is worst in spring (MAM CRPS 2.7× v3) — the Kalshi window.

**Tracked:** v4's complete artifact set is committed at `data/model_v4_artifacts/`
(parallel to v3) for the record — it is **not** production.

### 1.5 Production pivot: market_wing, flat-$, model-free (2026-05-29)

`market_wing + drop_lower_ask` (market anchor, no agreement) is the production
candidate. Established this session:

- **The v3 model is NOT used by market_wing.** Verified in code: anchor = market
  modal, `require_agreement=False`, `p_used = assumed_win_prob` (overrides the model
  PMF), `equal_payout` split, regime throttle bypassed under flat sizing. The PMF
  only feeds diagnostics. → the **same-day weather anchor (old P3) is obsolete for
  production** — the engine runs market-only, no weather, no model, no creds.
- **Sizing: flat-$, not Kelly.** Kelly@0.99 was an ~8× over-bet (its only size
  variation came from the throttle, which mis-fired on the 2026-04-16 tail). The
  `assumed_win_prob` sweep showed 0.92 halves the drawdown for ~90% of PnL; flat
  sizing ~tripled Sharpe at parity PnL/DD. Flat-$ ($2.50) chosen for the $25
  non-compounding account. **Caveat** (memory `project_sizing_flat_dollar`): flat-$
  re-risks during drawdowns and is in-sample-WR-dependent; **flat-% is the principled
  long-run choice** — revisit when scaling capital.
- **$25 scale validated (Phase 0):** net-positive at every flat-$ level; a fee-aware
  gate + `max_ask_sum 0.90` trim the ~10% fee-dead high-cost days (fee drag ~4% vs
  2.6% at $1000).
- **Engine wiring:** `ModelCfg.enabled` (config `false`) gates `run_cycle` to skip
  refresh/features/predict and use a uniform placeholder PMF; `_dispatch_strategy`
  routes `strategy.name` → `run_wing_strategy`. Replaces the hardcoded `joint_kelly`
  (a known loser). flat-$ + fee-aware added to `run_wing_strategy`.

### 1.6 Multi-city scope + execution study (2026-06-05)

**Deep history unlocked.** Kalshi's live `/markets`+`/trades` are windowed to ~67 days, but the
keyless **`/historical/*` tier** serves every settled market/trade back to 2021. `scripts/backfill_historical.py`
pulled the modern 6-bucket era (since 2023-01) for all 20 series into `data/backfill/<SERIES>/<date>.zip`
— **same schema as the live backfill**, so every existing backtest reads it unchanged. ~1,250 day-zips
each for the deep cities (Chicago, NYC); 3.4 years total. (Lesson **L11**: the "67-day cap" was a
property of how WE collected, not of the source.)

**Per-city verdict — only Chicago has a durable edge** (`scripts/per_city_synthesis.py`; 1 PM, top-2
wing, gate `sum_ask<0.90`, IS/OOS 80/20; edge = coverage−cost−fee, ¢ per $1). The honest column is
realistic OOS (`rOOS` = pay the next actual trade, out-of-sample):

| Tier | Cities | realistic OOS |
|---|---|---|
| **Durable** | **Chicago** (only) — `+5.0¢` rOOS, positive proxy edge in **all 4 years**, deepest sample | ✅ |
| Optimistic-only | Miami, LA, San Antonio, Seattle, Phoenix, Las Vegas — positive IS, **collapse OOS** | ❌ |
| Noise (IS<0, OOS>0) | Houston, New Orleans, DC, NYC, Philadelphia — small-OOS-tail, uncorroborated | ⚠️ |
| Never | Denver, Atlanta, Austin, Dallas, Minneapolis, Boston, OKC, SFO | ❌ |

The edge **decayed** for NYC/Miami/Denver as the market matured (positive 2023 → negative 2025-26);
Austin/Dallas never had one. Chicago alone is positive every year (`+3.0 / +2.0 / +2.9 / +7.3¢`).

**Maker execution does NOT rescue it** (`scripts/maker_fill_harness.py`). The taker (our limit-at-ask
order) pays the spread (~1-2¢/leg) — the one lever that could help is resting a bid (maker). The
backfill carries **`taker_side`** per trade (`yes`=lifted ask, `no`=hit bid), so a maker yes-bid fill
is simulable on the deep tape: it fills iff a `taker_side=="no"` sell prints at ≤ our bid in the
post-anchor window. Verdict over real trades (identical wing legs; IS/OOS):

- **Fills happen** — 38% (thin books: Chicago/NYC/Miami) to 89% (liquid: Phoenix/Seattle/Boston).
- **But they're adversely selected** — the WINNING leg fills less than the LOSING leg in **14/20**
  cities (Chicago 32% vs 42%; New Orleans 42% vs 74%; Las Vegas 68% vs 90%). The market sells into
  your bid when the bucket is going against you → you fill losers and miss payoffs, breaking the
  coverage wing.
- **For Chicago, maker is strictly worse than taking** (maker-IS `−11.8¢` vs taker `+1.2¢`; the
  winning leg fills just 32%). The few positive maker-OOS cities (Phoenix/Houston/OKC) are small-OOS
  mirages (IS doesn't corroborate). The fill model ignores queue/size → it is an **upper bound** on
  fills; real maker fills are lower and more adverse, so the negative verdict is conservative. (**L15**)

**Forward upgrade path.** `scripts/orderbook_logger.py` now logs **real bid/ask + depth ladders**
(`yes_book`/`no_book`) for all 20 series, accumulating on the droplet (204 markets/poll, 0 settled-day
zips yet). Once it has ~weeks of near-anchor depth it can re-test maker fills with real queue/size —
but it is very unlikely to flip a result this one-sided.

**Settlement stations — all 20 verified vs Kalshi's OWN rules** (`scripts/verify_stations.py`). The
newer `KXHIGHT*` series name the NWS Climatological Report code in `rules_secondary` (`CLI<xxx>` →
`K<xxx>`, authoritative); the older `KXHIGH*` series name the airport in `rules_primary`. Both
previously-flagged cities resolved: **AUS → `KAUS`** (Austin-Bergstrom, not Camp Mabry) and **DAL →
`KDFW`** (Dallas/Fort Worth, not Love Field); Houston settles on **Hobby `KHOU`**, not Bush. Zero
config changes — all 20 were already correct.

**Paper expanded to all 20 + read-only auth.** `config/paper.yaml` now lists all 20 cities; the paper
bot and the logger both authenticate the read-WRITE key used **read-only** (`main.py` authenticates a
paper read path when `KALSHI_KEY_ID` is in the env — SAFE: paper never calls `place_order`, guarded by
`is_live()` in `execution.py`; the key buys the higher rate-limit tier **only for SIGNED requests**).
Two layers protect the public read path (`fetch_event`), **added 2026-06-07** after it 429'd on
2026-06-06 (a GET burst at the top of the ET/CT anchor windows tripped `httpx 429` on PHIL/DC/BOS,
HOU×3/BOS×2 — each isolated per-market, recovered only on the next ~5-min tick):
(1) **Stagger** — `scheduler.inter_market_stagger_seconds` (0.5 s); both `run_cycle` and the startup
`preflight` preview sleep it between markets so a same-tz batch isn't simultaneous.
(2) **Retry** — `KalshiClient._get_with_retry` retries 429/5xx with a `Retry-After`-aware backoff
(6 attempts, ~9.5 s window) and only then raises. The retry is load-bearing: `fetch_event` is **unauthenticated**
(`self._http.get`, not signed), so the read-only-auth tier does **not** apply to it — the read shares
Kalshi's low per-IP tier with the always-on logger, and the stagger alone left **15/20 preflight reads
still 429'ing at 0.5 s**. With the retry, 429 → wait → retry → success: restart logs are clean and a
throttled anchor read no longer loses a city. (The order path's own 400-invalid_parameters retry,
commit `1bcf1f7`, is separate.) Deploy units carry an optional
read-only `EnvironmentFile` (`deploy/weather-alpha-paper.service`, `deploy/orderbook-logger-user.service`).

---

### 1.7 Net-of-ceil-fee OOS replay — does the edge survive the REAL fee? (2026-06-08)

The §1.6 `+5.0¢` Chicago headline (`per_city_synthesis.py`) is normalized **cents-per-$1-payout** and
uses a **continuous fee approx** (`FEE1 = 0.07·a·(1−a)`); its own docstring flags this as "a touch
optimistic" vs the live `ceil(7%·N·P·(1−P))`. An LLM-council review
([`tasks/council-transcript-2026-06-07-improve-vs-pivot.md`](tasks/council-transcript-2026-06-07-improve-vs-pivot.md))
asked whether the edge survives the real fee. `scripts/chicago_oos_netfee.py` replays the **exact
production config** (same kwargs as `backtest_multicity.run_one` / `config/live.yaml`:
`market_wing + drop_lower_ask`, flat $2.50, `fee_aware`, `max_ask_sum 0.90`, `assumed_win_prob 0.92`,
`wing_anchor=market`) on the 3.4-yr Chicago tape with the **real `weather_alpha.fees.trade_fee_cents`
ceil fee**, in actual dollars, 80/20 IS/OOS, under two fills: **proxy** (100% fill at last+1¢) and
**realistic** (first post-anchor print ≤60 min; day counts only if every leg printed).

**Answer: the edge SURVIVES the fee — but the fee was never the binding question.**

| Chicago edge, ¢/$payout | proxy IS | proxy OOS | real IS | **real OOS** |
|---|---|---|---|---|
| continuous fee (`per_city_synthesis`) | +2.5 | +5.6 | −2.2 | **+5.0** |
| real ceil fee (`chicago_oos_netfee`) | +2.1 | +5.1 | −2.6 | **+4.6** |

The real ceil fee costs a uniform **~0.4–0.5¢/$payout (~8%)** — headline +5.0¢ → **+4.6¢** realistic
OOS. But OOS it is **not statistically confirmed**: WR 89% vs per-day breakeven 85%, one-sided exact
**binomial p=0.11** (proxy 0.084); dollar t≈1.5; iid bootstrap CI **[−1.0, +10.6]**, P(≤0)≈**0.05**.
Three independent discounts, **none fee-related**: **(1) selection** — Chicago is the max of 20 cities
on the same tape (§1.6), so its OOS look is multiplicity-inflated; **(2) regime** — the OOS year is
*entirely* the post-2025 dense-liquidity era (realistic fill rose 22→33→98→100% by year), so
realistic≈proxy OOS only because adverse selection recently vanished; the **through-cycle proxy edge
is ~+2.5¢, not +5¢**; **(3)** the in-sample *realistic* edge is *negative* (−2.6¢, 34% fill). The fat
tail is **robust**, not a lottery — dropping the 3 worst OOS days *raises* the mean (5.1→7.8¢); that's
a position-sizing/ruin caveat, not a significance one. Dollars: realistic OOS **+$16 on flat $2.50**
over ~1 yr / 111 fires → scalable via size, not edge.

**Frequency correction.** The deep tape fires **~164/yr (~77 realistically fillable)** — the "24
fires" in §0 is a single-season $25 backtest count, not the annual rate. So the recent live "0 trades
in 5 days" is ordinary variance at the current ~27–40% fire rate (wings often cost ≥0.90 now), not a
fault — though local monitoring is blind (`data/{live_log.parquet,positions.json}` stale since
2026-05-30).

**Audit.** `stats-ml-logic-reviewer` independently reproduced every number — **SOUND-with-caveats, 0
CRITICAL**; flagged a pre-existing non-deterministic `_snapshot` tie-break (~$11/554-day drift vs
`run_one`, immaterial, affects the reference engine equally, left as-is). Bus artifact:
`tasks/_agent_bus/20260608-0513/stats-ml-logic-reviewer.md`. Pattern: **L17**.

**Bottom line.** Don't pivot the signal (real, clears fees) and don't fund yet — forward paper resolves
it nearly as well, since execution is currently clean (realistic≈proxy in this regime), so live adds
little the paper shadow can't. Treat +4.6¢ as "recent-regime, borderline," not "confirmed." The
cleanest resolver is genuinely-forward (post-2026-05-30) Chicago fires accumulated against this +4.6¢
benchmark — **not yet wired**: the paper bot generates fills on the droplet, but local persistence is
stale and no forward-edge rollup exists (a forward-tracking harness is the open follow-up).

---

### 1.8 Paper shadow forward-OOS — 20-city wing confirmed negative off-Chicago; 50% breaker latched (2026-06-09)

First local read of the resident **paper Book** (the FWD-TRACK sync now resolves §1.7's "stale local
persistence" caveat — `data/paper/positions.json` is current). It shows the paper bot **account-halted at
realized −$9.42**, and `scripts/diagnose_paper.py` (joins the Book to the `data/backfill` settlement tape)
nails why — **no code bug, structural negative edge:**

| paper cohorts (13 settled, all NON-Chicago) | n | avg P/L | total |
|---|--:|--:|--:|
| **cover** (a wing leg won) | 8 (62%) | +$0.26 | +$2.10 |
| **miss** (high outside the 2-leg wing) | 5 (38%) | −$2.30 | −$11.52 |
| **EV / cohort** | 13 | **−$0.72** | **−$9.42** |

The cheap 2-leg wing wins ~a quarter when right but loses ~the full $2.50 when wrong; 62% coverage can't
pay for that. **Chicago never fired** (wing too expensive every day — the same gate as LIVE, §FWD-TRACK),
so the entire bleed is the **other 19 cities = the no-edge universe of §1.6** → the live forward-OOS
confirmation that "only Chicago has a durable edge."

**`drop_lower_ask` does not generalize off-Chicago.** In **4 of the 5 misses the winning bucket was the
exact leg `drop_lower_ask` discarded** (the cheap adjacent); the full 3-leg wing would have covered 12/13
(the 5th, DAL 6/7, was a +2-bucket tail blow-out the 3-leg misses too). On Chicago the placebo
`drop_higher_ask` failed (−$431, §1.3) so dropping the lower-ask leg was right; off-Chicago it dropped the
winner. **Caveats:** N=5; and re-adding the 3rd leg pays an extra leg on *every* cohort — a backtest
question, not a confirmed fix.

**The 50% account breaker is correct, not a degenerate artifact.** `account_hwm=0`/`bankroll=null` look
wrong but aren't — realized peaked at $0 at inception and only fell, so drawdown is measured from 0. The
latch reconstruction matches the Book to the cent: it trips on the 2026-06-08 settlement sweep when
drawdown **$9.42 ≥ 50% of the running $15.58 balance** ($25 + realized). Latched until
`scripts/halt.py --reset --config config/paper.yaml`; the 6 open 6/8 positions (BOS/AUS/MIN) still settle.

**Overnight→noon coverage sweep** (`scripts/topk_overnight_2026.py` → `data/topk_overnight_2026.csv`):
top-1/2/3 market-rank coverage (truth = `settlement_value`, rank = last-trade `yes_price`; the
`scripts/modal_rank_multicity` conventions) for all 20 cities on 2026 events, **hourly 10 PM prev night →
12 PM noon local**. Coverage rises **monotonically**; pooled (N flat ~2787 — 2026 overnight markets are
already liquid) **top-1/2/3 = 47/75/89% at 10 PM → 58/86/96% at noon**. Desert cities (PHX/LV/HOU/MIA)
saturate overnight (95–98% top-3, barely improve); coastal/microclimate lag (SF tops out 44/77/90%).
Coverage-only (no cost term) — high late ≠ tradeable edge (price has risen to match; net-EDGE knee is
~2 PM per `data/anchor_grid.txt`).

**Balance note:** the LIVE account's $23.56 (adoption) → $17.82 drop is the operator's **manual trading**,
not the bot (LIVE Book: 0 fills, $0 realized, $0 fees, not halted).

---

## 2. Sizing & risk pipeline

```
stake_per_day = f_stake × bankroll
where  f_stake = min(kelly_full × kelly_fraction × throttle, total_exposure_max_pct)
       kelly_full ≈ assumed_win_prob − (1−assumed_win_prob) × sum_asks/(1−sum_asks)
       throttle ∈ {1.0, 0.5, 0.25}    (halve once per active condition)
```

| Param | Value | Source |
|---|---|---|
| `bankroll_usd` | $1000 (paper) | `config/weather_alpha.yaml` |
| `kelly_fraction` | 0.25 (quarter-Kelly) | config |
| `assumed_win_prob` | 0.99 | CLI `--wing-assumed-win-prob` |
| `max_ask_sum` | 1.00 | CLI `--wing-max-ask-sum` |
| `per_contract_max_pct` | 0.20 ($200 per leg cap) | config |
| `total_exposure_max_pct` | 0.60 | config |
| `regime_throttle.confidence_floor_top1` | 0.20 (halve if peak_P below) | config |
| `regime_throttle.hrrr_persistence_gap_max_f` | 8 °F (halve if exceeded) | config |
| `assumed_spread_cents` | 2 (sim ask = mid + 1¢) | CLI |

**Throttle is doing real work:** the second-worst loss day (2026-05-12, -$180) was already halved by the HRRR-persistence-gap throttle (gap = 23 °F). Without it, the loss would have been ~-$360.

**The unavoidable tail (1 day per ~22 trades):** when truth lands outside the bought wing on a "calm" day where both model confidence and HRRR-persistence agree, no per-trade gate stops it. Worst observed: 2026-04-16, -$181 in `wing + drop_lower_ask` (-$368 in `market_wing` due to larger compounded bankroll).

**Drawdown circuit-breakers (wired 2026-06-01, multi-market).** Orthogonal to per-trade sizing: the `Book` latches a **per-city halt at 25%** drawdown of the running account and a **whole-account halt at 50%**, both measured from the cumulative-realized peak and held until manual reset (`scripts/halt.py --reset … --config <cfg>`). A halted city stops opening new trades but **still settles** prior-day positions. `total_exposure_max_pct` is also now enforced in `execute()` (Σ open exposure + the new leg ≤ pct × balance, shared across all markets in the process). Sizing itself is unchanged — flat $2.50/trade. See §7 `MULTIMKT`/`REVIEW`.

---

### 2.1 Sizing-mode study — flat $ vs %-of-bankroll vs Kelly (2026-05-31)

Chicago @ 1 PM, fee-aware, in-sample ~25 fires (`scripts/sizing_modes.py`; $2.50 == 10% of the $25 start, so the first bet matches):

| sizing | final $ (from $25) | return | maxDD % |
|---|---|---|---|
| flat $2.50 | 33.44 | +33.8% | 6.8% |
| 5% of bankroll | 30.44 | +21.8% | 5.0% |
| 10% of bankroll | 34.94 | +39.8% | 10.7% |
| 25% of bankroll | 56.42 | +125.7% | 26.7% |

- **Loss profile under current prod (flat $2.50): uniform full-stake losses.** A wing loss is always a *full* miss (truth outside the 2 buckets → forfeit the whole stake; held to settlement, no partial recovery). Flat-$ pins every bet to ~3 contracts (~$2.2–2.6 deployed), so **every loss is ~−$2.3 to −$2.5 — there are no "small" losses.** What varies day-to-day is the **win** size: +$0.2 (expensive wing) to +$0.7 (cheap wing), since win margin = (1 − cost). The Chicago log bears it out: 25 fires → 24 wins of +$0.2–0.7 and **one** loss of −$2.26. (The ~−$1 "small losses" appear only under the *old* Kelly/throttle sizing, where the **bet** varied — they do **not** occur in current prod.)
- **flat → % doesn't improve the *deal*** — the return/drawdown ratio is ~fixed (≈4–5). % sizing only (a) **compounds** (geometric growth if the edge persists over many fires) and (b) makes per-trade risk **float** with the bankroll (the fixed-$ loss cap dissolves).
- **Kelly is the trap.** Full-Kelly ≈ **79%** of bankroll at the *measured* 96% coverage, but it is hyper-sensitive to the win rate — the thin 67-day estimate is the whole exposure:

| true coverage | full-Kelly | 25% of bankroll is |
|---|---|---|
| 96% (measured) | ~79% | ⅓ Kelly (sub) |
| 90% | ~47% | ½ Kelly |
| ~86% | ~25% | = full Kelly (cliff) |
| 82% | ~4% | ~6× → geometric ruin |

**Discipline (load-bearing):** stay on **flat $2.50** through the forward-confirmation phase — fixed, nameable risk and a clean read on whether 96% is real. Scale only *after* the edge is confirmed forward, and even then use a modest fractional % (≪25%): most of the growth, a fraction of the drawdown. The high win rate makes aggressive sizing tempting **and** most fragile; compounding an unconfirmed edge amplifies the estimation error geometrically.

---

## 3. Pipeline / execution order (every gate, no hidden filters)

| # | Stage | What | Skip reason if failed |
|---|---|---|---|
| 1 | Backtest loop | `oof_fold >= 0`, no NaN PMF, `date in cli_truth`, snapshot non-empty | data pre-filters |
| 2 | Wing | ≥3 live Kalshi contracts | "need >= 3 live contracts" |
| 3 | Wing | Identify model_modal (argmax bucket_prob) and market_modal (argmax yes_ask) | (step, not a gate) |
| 4 | Wing | **`require_agreement`** | "model-market disagreement" |
| 5 | Wing | Build wing = [anchor, pos-1, pos+1] (anchor = model_modal by default) | (step) |
| 6 | Wing | `len(wing) >= min_wing_size` (2 if any drop_X, else 3) | "modal at edge of layout" |
| 7 | Wing | **`drop_lower_ask`** transform (keep modal + higher-ask adj) | (transform) |
| 8 | Wing | `sum_asks < max_ask_sum` | "sum_asks >= max" |
| 9 | Wing | `ev_margin = assumed_win_prob - sum_asks >= min_ev_margin` | "EV margin < min" |
| 10 | Wing | Regime throttle (halve × halve) | scales sizing, never blocks fire |
| 11 | Wing | Kelly + caps (`per_contract_max_pct`, `total_exposure_max_pct`) | "kelly <= 0" |
| 12 | Wing | Per-leg integer rounding, ≥2 legs after caps | "only N legs after caps" |
| 13 | Settlement | `won = bucket_contains(actual_F, spec)`, fee = `ceil(7% × n × p × (1-p))` | — |

---

## 4. Model side (carry-over)

Self-contained notebook: fetch → features → walk-forward CV → calibration →
deployable ensemble → evaluation → artifact save.

- **§3 Feature engineering** — 46 features across 7 groups (astro, climatology+persistence, TAF peak, METAR @ T, ASOS, HRRR base+path-5, hard-boundary running max).
- **§5 Walk-forward CV** — monthly test folds, 1-day gap, ≥12-month min train, 3 seeds × 19 quantiles.
- **§6 Quantile → PMF** — Chernozhukov rearrangement, integer-°F grid.
- **§8 Calibration** — season-selective isotonic (JJA only; DJF/MAM/SON pass through raw).
- **§9 Deployable ensemble** — final models on all rows, packaged in `data/model_v3_artifacts/`.
- **§10.2 Bucket accuracy** — 2°F fixed-pair, model-only. 45.8% / 87.8%.
- **§10.3 NEW: Kalshi-resolution accuracy** — model vs market modal, split by agreement. Added 2026-05-28.

Key §10.3 findings (2026-Q2, n=61):

| Subset | Model Top-1 | Market Top-1 | Model Top-3 | Market Top-3 |
|---|---|---|---|---|
| All days | 44.3 % | **63.9 %** | 91.8 % | **98.4 %** |
| Agreement (n=34) | 67.6 % | 67.6 % | **100.0 %** | **100.0 %** |
| Disagreement (n=27) | 14.8 % | 59.3 % | 81.5 % | 96.3 % |

→ The market beats the model unconditionally. The wing strategy works because on agreement days both signals point the same direction; on disagreement days the model is catastrophic (14.8% Top-1) so skipping them is correct.

### Retraining
`model_v3.ipynb`, **Run All** (~21 min). Final cell saves to `data/model_v3_artifacts/`.

---

## 5. Production code (weather_alpha package)

| Module | Role |
|---|---|
| `weather_alpha/strategy.py` | All 8 strategy functions; `run_wing_strategy` is the production code path (flat-$, fee-aware) |
| `weather_alpha/calibration.py` | LOO α blending (model-market); currently α=0 (market-only) per LOO fit; unused on the model-free path |
| `weather_alpha/live_fetchers.py` | Async live data (METAR/TAF/ASOS/HRRR/CLI) for the running bot |
| `weather_alpha/engine.py` | Orchestrator; `run_cycle` iterates `cfg.markets` under one shared Book → `list[CycleResult]` (per-market isolation); `verify_bankroll` (LIVE balance), `_tradeable_contracts` event guard, `settle_market_if_due` per-station settlement, `_latch_halts` drawdown |
| `weather_alpha/positions.py` | `Position`(+`station`) + shared `Book` — per-city/account drawdown HWM + latched halts (`update_drawdown_halts`/`reset_halt`/`backfill_station`/`is_halted`) |
| `weather_alpha/report.py` | Concise operator terminal stream (ENTER/SKIP/FILLED/SETTLED/HALTED) for the resident `--loop` bot |
| `weather_alpha/tui.py` | Textual TUI for live monitoring (⚠️ still an active trading driver — see §7 `TUI`) |
| `weather_alpha/main.py` | Entry point — resident `--headless --loop` multi-market loop (+ single-tz model loop) |
| `weather_alpha/scheduler.py` | `Scheduler` (single-tz: refresh/anchor/intraday, model path) + `MarketAnchorScheduler` (per-tz, resident bot); state persisted across restart |
| `weather_alpha/pmf.py` | Bucket parsing, PMF utilities, Chernozhukov rearrangement |
| `weather_alpha/fees.py` | Kalshi fee formula |
| `weather_alpha/kalshi.py` | `KalshiContract` + parser (`subtitle`/`yes_sub_title`, `between`/`less`/`greater`); locale-safe `event_date_code()` |
| `weather_alpha/model.py` | Artifact loader, `Prediction` dataclass |
| `weather_alpha/config.py` | YAML loader — `markets:` list + `MarketCfg`/`RiskCfg` drawdown pcts, `config_path`, `load_config(require_live_creds=)` |
| `config/weather_alpha.yaml` | Default single-market config; multi-market in `config/{live,paper}.yaml`, per-tz one-shots in `config/{chicago_live,houston_paper}.yaml` |

---

## 6. Reproducing the production backtest

```powershell
python scripts/backtest_strategy.py `
    --strategy wing `
    --wing-drop-lower-ask `
    --wing-assumed-win-prob 0.99 `
    --wing-max-ask-sum 1.00 `
    --out data/prod_backtest.parquet
```

Expected: 22 fires, +$308 PnL, 20/2 W/L. Positions land in `data/prod_backtest_positions.parquet`.

### Other reproducible scripts
- `scripts/all_variants_retest.py` — full 12-variant comparison
- `scripts/market_wing_test.py` — 3 market_wing variants
- `scripts/drop_lower_ask_oos.py` — 3-part OOS validation (early/late/placebo)
- `scripts/sizing_breakdown.py` — per-day stake decomposition
- `scripts/verify_bucket_accuracy.py` — §10.2 verification + §10.3 computation
- `scripts/full_67day_backtest.py` — filtered vs unfiltered comparison on full Kalshi window
- `scripts/v3_v4_kalshi_compare.py` — v3-vs-v4 model & market accuracy at Kalshi resolution, both anchors (§1.4)
- `scripts/wing_settlement.py` — 1/2/3-leg wing settlement % by model & anchor hour (§1.4)
- `scripts/market_modal_coverage.py` — market top-1/2/3 modal coverage, midnight vs 1 PM (§1.4)
- `scripts/v3_v4_why.py` — quantifies the v3-vs-v4 skill gap (CRPS, sharpness) + information mechanism (§1.4)
- `scripts/backtest_strategy.py --model-dir <dir> --anchor-hour-local <H>` — backtest any model dir at any anchor hour; OOF-only mode if the dir lacks deployable artifacts
- `scripts/chicago_oos_netfee.py [SERIES]` — net-of-**REAL-ceil-fee** OOS replay of the production config on the Chicago (or any) tape, both fills (proxy/realistic) + binomial-vs-breakeven (§1.7)
- `scripts/forward_edge_tracker.py [--mode --series --since --benchmark]` — accumulate post-go-live REALIZED fires from `live_log.parquet`, settled vs the backfill truth, into a running edge vs the §1.7 +4.6¢ benchmark (the `FWD-TRACK` rollup; blocked on the stale local pull)
- `scripts/diagnose_paper.py` — why the paper Book is down/halted: joins `data/paper/positions.json` to the `data/backfill` settlement tape, classifies each city-day wing cover/miss, decomposes EV, and reconstructs the 50% drawdown-halt latch point (§1.8)
- `scripts/topk_overnight_2026.py [SERIES…]` — overnight→noon (10 PM prev night → 12 PM, hourly, local) top-1/2/3 market-rank coverage sweep, all 20 cities, 2026 events → `data/topk_overnight_2026.csv` (reuses `scripts/modal_rank_multicity` conventions; §1.8)

---

## 7. Open items

| # | Task | Status / Notes |
|---|---|---|
| P1 | **Wire production strategy into the engine** | ✅ DONE — config dispatch → `market_wing + drop_lower_ask` + flat-$ sizing, replacing `joint_kelly`. Model-free path (`ModelCfg.enabled=false`) verified end-to-end in paper on live data. |
| L1 | **Deploy the orderbook logger always-on** | ✅ DONE 2026-06-02 — running on the DO droplet (`orderbook-logger-user.service`, `systemd --user`), logging **both** KXHIGHCHI + KXHIGHTHOU depth, survives reboot. Host + the local-pull pipeline: see **DEPLOY**. |
| DEPLOY | **Always-on deployment (DigitalOcean) + local orderbook pull** | ✅ DONE 2026-06-02. Host: DO droplet (NYC1, 1 vCPU / 1 GB, Ubuntu 24.04), user `weather-alpha`, **sparse + `--filter=blob:none` partial** checkout at `~/weather-alpha` (nested `0700` subdir — root + the `weather-alpha` user only; cone `weather_alpha config scripts deploy`, **never the full repo** — lessons L20), set up creds-free via `deploy/setup-local.sh` (+ 2 GB swap for the pip install on the 1 GB box). Running 24/7 under `systemd --user` + `loginctl enable-linger` (survive logout/reboot): **`weather-alpha-paper.service`** (CHI+HOU, `config/paper.yaml`) + **`orderbook-logger-user.service`** (KXHIGHCHI+KXHIGHTHOU depth → `data/orderbook`). **Local pull:** a daily Windows Task-Scheduler job (`PullOrderbookZips`, 08:00, `StartWhenAvailable`) runs `scripts/pull-orderbook-zips.ps1` in **move-mode** (`OB_MOVE=1`) — copies each settled-day `<series>/<date>.zip` to the local box, size-verifies, then deletes it from the VM (cross-platform pair `scripts/pull-orderbook-zips.{sh,ps1}`; `deploy/README.md` → "Pull settled-day zips"). Current droplet: `weather-alpha@137.184.128.37` (migrated from user `fa` 2026-06-08, then relocated flat → nested `~/weather-alpha` (`0700`) the same day; lean sparse/partial checkout; `fa` deleted; runbook `deploy/migrate-to-weather-alpha.md`). **`weather-alpha-live.service` STARTED 2026-06-02 04:08 UTC** — RW key uploaded to `secrets/` (env + PEM, 600), read-only auth pre-flight PASSED, bot adopted $23.56 and watches CHI; first real order at the next 1 PM CT anchor (**L2**). |
| SEC | **Secrets / leakage audit + `.gitignore` hardening** | ✅ 2026-06-02. Full audit (current tree + all 49 commits / branches): **no leakage** — the RSA private key (PEM) was never committed (0 `PRIVATE KEY` blocks), no real `KALSHI_KEY_ID` committed (only `.env.example`/docs placeholders), no SSH private key in history; repo is **private**; key material never echoed in chat. PEM on the VM is `600`/`fa`-only + gitignored → **no rotation needed**. One latent gap (never realized): `.gitignore` had `*.pem` but not the directory, so `secrets/kalshi-rw.env` + `secrets/readwrite-key-id` (key id) were stage-able on the VM. Fixed — ignore `secrets/` + `*.env` (+ runtime `data/{live,paper,orderbook}/`); committed `1031c23`, pushed, and **pulled on the VM** (verified `secrets/` now ignored, `git status` clean, bot still active). Pattern: **L8**. |
| L0 | **Order-safety hardening (pre-live review)** | ✅ DONE 2026-05-30. Engine+stats review (`tasks/review_engine_logic.md`, `tasks/review_backtest_stats.md`). All 4 CRITICALs fixed: C1 LIVE fill-confirmation (book actual fills via `get_positions`, not assumed), C4 deterministic `client_order_id` (no dup orders on retry), C3 per-leg kill-switch + intraday outlay breaker (`Book.daily_outlay_cents` vs `daily_max_loss_usd`) + `per_anchor_max_trades`. Kill switch: `python scripts/kill.py` (arm/disarm/status). Verified: `scripts/check_live_execution.py`. **Stats verdict: edge real (placebo passes) but thin + front-loaded → $2.50 toy forward-test only, don't scale.** |
| AUTH | **Kalshi auth signing-path + portfolio-schema fixes** | ✅ DONE 2026-05-31. This session's engine audit found two LIVE-blockers in `kalshi.py`: (1) the RSA-PSS signature signed the path **without** the `/trade-api/v2` prefix that `api_base` already carries → every authenticated call would 401; now derives `_base_path` from `api_base` and signs the full path. Latent in PAPER (only the unsigned public `/markets` was exercised). (2) `get_balance`/`get_positions` read the now-removed integer fields `balance`/`position`/`market_exposure`; now read the fixed-point `balance_dollars`/`position_fp`/`market_exposure_dollars` (parsed → cents) with a legacy fallback. **Validated live** — both RO+RW keys PASS `check_kalshi_auth.py`, balance parses to $23.56. Local proof: real RSA sign/verify + parser checks. Caveat: `position_fp` parsing of a *non-zero* holding is unexercised (0 open positions) — first real test on the first live position. |
| KILL | **Kill switch no longer freezes settlement** | ✅ DONE 2026-05-31. Arming the kill switch (or hitting the daily-loss cap) raised `KillSwitchTripped` at the top of `execute()`, which `run_cycle` let propagate **before** the settlement block — so a halted bot also stopped settling prior-day positions and re-syncing bankroll. Fix: `run_cycle` catches `KillSwitchTripped`, records a no-trade `ExecutionResult`, and falls through to settlement; new orders stay blocked (execute raises before placing anything). Verified: armed switch → 0 new fills but the prior-day position settles (+119¢ in the test). |
| AUDIT | **2026-05-31 live-path audit — remaining findings** | 8-finding engine audit this session. Fixed: #1/#2 auth + portfolio schema (**AUTH**), #5 kill-switch vs settlement (**KILL**). Already tracked: #3 late-fill booking + #7 no cycle-start reconcile (**RECON**/L3; `README_PROD` known-limit #2), #6 TUI duplicate driver (**TUI**). Open, low severity: **#4** the one-shot catch-up can trade off-anchor if a missed run fires late while the event is still open (documented in `deploy/weather-alpha.timer`; opt out via `Persistent=false`); **#8** the intraday outlay cap (`Book.daily_outlay_cents` vs `daily_max_loss_usd`) counts contract cost but not fees — immaterial at flat-$2.50 on $25 vs a $100 cap, and the realized daily-loss gate already includes fees. |
| AGREE | **Model–market agreement as a fire-gate / sizing signal (deferred 2026-05-31, user-parked)** | Backlog — own task. The original production strategy *required* agreement (`model_modal == market_modal`): 22 fires, 91% WR, +$308, **Sharpe 2.85** (vs `market_wing` 1.66); agreement days showed ~100% Top-3 coverage (see §1.3, §4/§10.3). Dropped in the 2026-05-29 model-free pivot for **ops simplicity, NOT lack of edge**. Two uses to test: **(a) gate** (fire only on agreement, as before), **(b) size up** on agreement days. **Decide first:** is agreement *orthogonal* to the market price (v3 uses HRRR/METAR the market may underweight) or *redundant* with high `sum_asks` (already priced → no edge, per the 2026-05-31 calibration + sizing tests in `scripts/sum_calibration.py`, `size_by_sum.py`, `confidence_test.py`)? Decisive test (Chicago, via v3 artifacts): does agreement raise wing coverage at **fixed `sum_asks`**? **Caveats:** Chicago-only (v3 is KMDW-specific; other cities need their own models); reintroduces the weather/model stack the model-free pivot removed; the 2.85 Sharpe was 22 days → OOS-validate like everything else. |
| MULTIMKT | **Multi-market refactor — one resident bot for all live cities (+ one paper bot)** | ✅ BUILT + HARDENED 2026-06-01 (Phases 1–6 + two review passes; plan [`tasks/multimarket_refactor_plan.md`](tasks/multimarket_refactor_plan.md), review [`tasks/engine_code_review_2026-06-01.md`](tasks/engine_code_review_2026-06-01.md)). Engine trades a **`markets:` list** under one shared Book/bankroll (mode is per-process → no live/paper mixing), returning `list[CycleResult]` with **per-market failure isolation**; **per-station settlement** (each city resolves vs its own CLI station); **drawdown halts** — per-city **25%** / whole-account **50%** of the *running* balance, latched from the peak until reset (`scripts/halt.py --reset … --config <cfg>`); **exposure cap** (`total_exposure_max_pct`) now enforced in `execute()`; **terminal reporter** (`weather_alpha/report.py`: ENTER/SKIP/FILLED/SETTLED/HALTED lines); **`MarketAnchorScheduler`** fires each city at *its own* local 1 PM across any US tz from one resident `--headless --loop` process (60-min post-anchor window, no off-anchor catch-up; the single-tz `Scheduler` still serves the model path); the `kalshi.py` parser now reads `yes_sub_title` + `between`/`less`/`greater` strikes (e.g. Houston `KXHIGHTHOU`), and `event_date_code()` is locale-safe. New files: `report.py`, `config/{live,paper}.yaml`, `deploy/weather-alpha-{live,paper}.service`, `scripts/halt.py`, `tests/test_multimarket.py` (**28 tests pass**; run directly or `pip install -e .[dev]` for pytest). **Sizing unchanged — flat $2.50** (drawdown stops are an orthogonal gate, not a sizing change). Back-compat: `chicago_live.yaml`/`houston_paper.yaml` (single-market, Option A per-tz one-shot timers) still load + run. **Two review passes complete** — all 2 CRITICAL + 7 WARN + 8 INFO + 9 follow-up findings fixed (see **REVIEW** row). **Phase 7 (running):** the PAPER bot (`config/paper.yaml`, 2 cities) is now live 24/7 on the DO host (**DEPLOY**) — watch ~1 week before migrating any city LIVE. |
| REVIEW | **Engine code review (multi-market hardening) — all findings fixed** | ✅ DONE 2026-06-01. Two adversarial passes on the multi-market engine: **2 CRITICAL + 7 WARN + 8 INFO + 9 follow-up** findings, all fixed + verified ([`tasks/engine_code_review_2026-06-01.md`](tasks/engine_code_review_2026-06-01.md) has a Resolution section — don't rewrite it). Net hardening: the kill switch now reaches the live bot (kill/halt require `--config` + load with the cred check OFF, so they work from a credless shell); exposure cap enforced in `execute()`; LIVE sizing/cancel use exchange truth (`get_positions`); paper bankroll no longer double-counts settled fees; one bad market can't crash the others or trade off-anchor; per-anchor cap counts order *attempts*; locale-safe `event_date_code`; daily-loss cap default 100→15. **One DEFERRED:** the TUI is still an active trading driver — see the **TUI** row. |
| L2 | **Go live** | ✅ **ARMED 2026-06-02** (first fill pending at the next anchor). Kalshi RSA creds **validated live** (read-only `scripts/check_kalshi_auth.py` PASSES with RO+RW; balance $23.56, RSA-PSS signing OK; two LIVE-blocking auth bugs fixed first — **AUTH**). Read-WRITE key on the VM (`secrets/kalshi-rw.env` + PEM, 600); **`weather-alpha-live.service` enabled + running** (Chicago, `config/live.yaml`), adopted the real **$23.56** bankroll via `get_balance()`, 0 open positions, model-free flat $2.50. A PAPER bot shadows CHI+HOU. **Still UNEXERCISED until it fires: the LIVE order-placement + real-fill path** — first real order at the **next 1 PM CT anchor** (the irreducible "first live trade" unknown, now imminent). Kill: `.venv/bin/python scripts/kill.py --config config/live.yaml` (halts before the next order; still settles). |
| L3 | **Deeper reconciliation (post-first-trade)** | ✅ Mostly DONE 2026-05-30 (commits `1c6b679`, `292fd5d`): bankroll verified from `get_balance` at activation + re-synced after settlement (W1/W5); scheduler state persisted across restart (W2); event-selection guard (W3); per-leg `place_order` error handling (W4). **Still TODO:** see the **RECON** row below (Book↔exchange reconciliation, the C1/W5 tail) + a write-ahead order-intent log (I1). |
| D1 | **Model-free server is self-contained** | ✅ DONE 2026-05-30 (commit `ff4101c`): a model-free run needs **no model artifacts and no weather/herbie stack**. `refresh_data` no-ops; `run_cycle` drops `load_bundle`; new `engine.settle_if_due` pulls the CLI high on demand only when a prior-day position is unsettled (`anchor_date < today`); new `engine.preflight` = read-only activation snapshot (wallet/positions/market/strategy). `requests` moved to base deps. Verified on a Python 3.14 venv (18/18 imports; e2e cycle with neither `data_dir` nor `model_dir`). |
| D2 | **Bot runtime = daily one-shot (systemd timer)** | Scaffolded 2026-05-30: `deploy/weather-alpha.service` (`Type=oneshot`) + `weather-alpha.timer` (`OnCalendar 13:05 America/Chicago`, `Persistent`). One run trades today + settles yesterday + exits. Chosen over always-on `--headless --loop` (kept as the optional "rich" mode for intraday retries / a dashboard host). Creds via systemd `EnvironmentFile` (read-**write** key). Runbook: `deploy/README.md` → "Live bot". |
| TUI | **⚠️ NEXT SESSION — make the TUI a pure read-only monitor (the one DEFERRED review item)** | Today the TUI is **not** passive: its 60s tick uses the old single-market `Scheduler` to run `run_cycle` and place orders (a duplicate driver). This is the single deferred finding from the 2026-06-01 engine review (**REVIEW**). Refactor: strip the scheduler/order path; read **account-truth P/L from Kalshi `/portfolio`** — `balance`, `portfolio_value` (live mark), per-market `realized_pnl_dollars` + `fees_paid_dollars`, and **unrealized = `portfolio_value − Σ market_exposure`** (no labeled field; `/portfolio/positions` is per-market AND per-event). Poll ~1 min, decoupled from the Book, **read-only** key. (`KalshiClient` now reads the fixed-point `position_fp`/`market_exposure_dollars`/`balance_dollars` fields with a legacy fallback — see **AUTH** — but `KalshiPosition` still exposes only contracts + avg price; for the monitor, extend it to also carry `realized_pnl_dollars`/`fees_paid_dollars`.) |
| RECON | **⚠️ NEXT SESSION — Book↔exchange reconciliation (its own unit)** | `reconcile_with_exchange(cfg, book, client)` at activation + live cycle-start: correct known positions to the exchange qty; **adopt** unknown exchange positions (enrich `bucket_spec` via `fetch_event`, `anchor_date` via the ticker pattern, fee estimated); leave Book-only positions for the CLI settlement path. Closes the C1/W5 tail (an unbooked mid-exception fill) and stops the delta logic from over-buying. Needs a live authenticated account + its own mock-client tests. Tracked as task #20. |
| P2 | **Liquidity verification** | **Volume half ✅ ample (2026-06-02)** — trade-tape check (`scripts/liquidity_check.py` on `kalshi_history.parquet`, 446K prints / 67 events): in the 0–60 min post-anchor window, mid-priced buckets carry a **median ~1,507 contracts/leg** (min ≥4) vs the wing's ~2/leg need — 100% of legs clear it, and the whole leg order is smaller than one median trade print (5 contracts). Fill-by-volume is a non-issue at $2.50; the 1–2 PM window holds ~6.5% of daily volume. **Price/depth half still open (needs ladders, not trades):** ~19% of days (13/67) had no mid-bucket trade *printed* in the exact window (trade-absence ≠ quote-absence — the bot is a taker hitting a resting ask), and the **`mid+1¢` fill PRICE is unvalidated** since the trade tape has no resting bid/ask. Re-run a depth-based check once `scripts/orderbook_logger.py` has ~2 weeks of near-anchor depth ladders (`DEPLOY`). **Update 2026-06-05:** maker-fill feasibility now tested on the deep tape via `taker_side` (`scripts/maker_fill_harness.py`, §1.6) — posting at the bid fills but is adversely selected, so the limit-at-ask **taker** path stays; the forward depth ladders (logger now on all 20) refine the fill-PRICE once ~weeks deep. |
| EDGE | **Multi-city edge map (deep history)** | ✅ 2026-06-05 — `/historical/*` backfill (3.4 yr × 20 cities, `scripts/backfill_historical.py`) + per-city OOS synthesis (`scripts/per_city_synthesis.py`): **only Chicago durable**; others collapse OOS or decayed as the market matured. §1.6. |
| FEE-OOS | **Net-of-ceil-fee OOS replay (does the edge survive the REAL fee?)** | ✅ 2026-06-08 — `scripts/chicago_oos_netfee.py` replays the EXACT production config on the 3.4-yr Chicago tape with the real `ceil()` fee in $: edge **survives** (realistic OOS +5.0→**+4.6¢**, ~8% drag) but is **NOT statistically confirmed** (binomial p=0.11; bootstrap P(≤0)≈0.05; selection [Chicago=max of 20] + regime [OOS=recent dense-liquidity only; through-cycle ~+2.5¢]). Fire rate **~164/yr** (the §0 "24" is a single-season count). Audited SOUND-w/-caveats. §1.7 / **L17**. |
| FWD-TRACK | **Forward-edge rollup (accumulate post-go-live fires vs the +4.6¢ benchmark)** | ✅ WIRED + LIVE 2026-06-08. **Tracker:** `scripts/forward_edge_tracker.py` reads the resident bot's per-mode `data/<mode>/live_log.parquet` (default `paper`; real fills/fees), settles each fire vs the backfill `settlement_value` (via `load_city`, no new auth), and reports running edge + N + WR + binomial-vs-breakeven + z-test vs §1.7's +4.6¢ + ~fires-to-significance, with a `positions.json` Book cross-check. **State sync:** `scripts/pull-state.ps1` + `scripts/merge_state.py` pull both `data/{paper,live}/` state — `live_log.parquet` **rotated→moved** off the droplet (atomic `mv`→shard→pull→byte-verify→remote-delete; bot recreates it; merged into the canonical local log, dedup), `positions.json` **copied** (the live Book is LEFT on the droplet). Scheduled task **`PullBotState`** registered (daily 08:10 CT; `-StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries`; stable WindowsApps `pwsh` alias — bare `pwsh.exe` 0x80070002-fails under Task Scheduler; LastTaskResult 0). First pull landed **paper 394 rows, live 36 rows, anchors Jun 2–7**. A tracker bug — reading the 20-city paper log **without filtering by series**, so other cities' legs matched against Chicago's winner (bogus 0% coverage) — was caught + fixed (series ticker-prefix filter) once the June backfill enabled scoring. **Real state: Chicago has fired 0 times forward** — paper is `no_target` 6/6 days and live is 0 fills; the −$4.99 paper realized P&L is the **other 19 cities**, not Chicago. **Why 0 (diagnosed, not a bug):** every day the top-2 adjacent buckets price to **sum ~0.97–1.03** — an efficiently-priced two-bucket coin-flip — so the wing is negative-EV (`ev_margin<0`) and correctly skipped (`sum_asks<0.90`). Loosening the gate wouldn't help (they clear even the 0.92 EV bound). The wing only fires on a *confident-modal + cheap-adjacent* day (sum<0.90); early-June Chicago has been split-regime, so the forward-significance clock ticks slowly/irregularly (mechanism behind §1.7's low recent fire rate). Recent Chicago backfill was extended to **2026-06-06** (run read-only on the droplet, pulled local) so truth is ready the moment Chicago fires. Net: forward Chicago sample is still **N=0** — the system is wired and correct, but there is nothing to score yet. §1.7. **Update 2026-06-09:** local paper state now synced (resolves the stale-pull blocker); paper realized −$4.99→**−$9.42** and the bot **latched its 50% account halt** — diagnosed structural-negative off-Chicago (`scripts/diagnose_paper.py`): 8 cover/5 miss, EV −$0.72/cohort, `drop_lower_ask` dropped the winner 4/5 misses; Chicago forward sample still **N=0**. §1.8. |
| DATA-LOC | **Local data relocated out of the repo + droplet kept lean (2026-06-08)** | ✅ Raw market data → `..\data\weather\orderbook` (`OB_LOCAL_DIR`); **truth tape** → `..\data\weather\backfill` (moved out; repo `data/backfill` is now a **directory junction** → `load_city` unchanged); **bot logs stay in the repo** (`data/{paper,live}`). New automation **`RefreshTruthTape`** (daily; `scripts/refresh-truth-tape.ps1` → droplet `backfill_cities` → move-pull → delete). All three pulls (`PullOrderbookZips` / `PullBotState` / `RefreshTruthTape`) follow **zip → move → delete** (verify local, then delete the droplet copy) so the droplet holds only the live Book + the in-progress day's raw. §9.1, `deploy/README.md`. |
| PROBE2 | **strat_prod_2 model-free probe (branch-local)** | ✅ 2026-06-08 ([`tasks/strat_prod_2_probe.md`](tasks/strat_prod_2_probe.md)). MM spread-capture + running-max ratchet extended to **N=6 — both confirmed dead** (added IEM-ASOS obs fallback + UTC→local-day fix in `ratchet_probe.py`). Model-free idea batches #1 (calibration, day-of-week, interior/tail) + #2 (cross-city synchrony, long-confirm) — **5/5 dead**; market efficient on these axes (re-validates #2). Recommendation: pause model-free probing; the binding work is confirming the wing forward (**FWD-TRACK**). Branch `strat_prod_2`, kept local. |
| MAKER | **Maker-execution feasibility (the spread lever)** | ✅ TESTED + CLOSED 2026-06-05 — `scripts/maker_fill_harness.py` on the deep tape (`taker_side`): posting at the bid fills (38–89%) but is **adversely selected** (winning legs fill less than losing in 14/20; Chicago maker strictly worse than taker). Doesn't open the 19 or fatten Chicago. Forward depth ladders (logger, all 20) will refine. §1.6 / **L15**. |
| STATIONS | **Settlement-station verification (all 20)** | ✅ 2026-06-05 — `scripts/verify_stations.py` vs Kalshi `rules_primary`/`rules_secondary` (the `CLI<xxx>` code): all 20 correct, **0 changes**; AUS=`KAUS` (Bergstrom not Mabry), DAL=`KDFW` (not Love Field), HOU=`KHOU` (Hobby not Bush). §1.6. |
| PAPER20 | **Paper expanded to all 20 + read-only auth** | ✅ 2026-06-05 — `config/paper.yaml` = 20 cities; paper bot + logger authenticate the RW key **read-only** (`main.py` paper read-auth — SAFE: paper never places orders; logger keyless→auth, now logs all 20 series / 204 mkts per poll). Cycles iterate markets sequentially (no 429); only the startup preflight's 20-call burst 429s (cosmetic). Deploy units updated (`deploy/weather-alpha-paper.service`, `deploy/orderbook-logger-user.service`). §1.6. |
| ~~P3~~ | ~~Same-day live anchor~~ | **OBSOLETE for production** — market_wing is model-free (§1.5), so no same-day weather feed is needed. (Free real-time sources verified anyway: NWS `metar_substitute` / AviationWeather are token-free; precision = the hourly METAR T-group.) |
| P4 | **Tail-risk** | Largely addressed by flat-$ sizing (no over-bet); the 04-16-type tail is now capped at the flat stake. |
| P5 | **Expose flat-$ in the backtest harness** | The +$7.98/$25 production figure uses flat-$ + fee-aware sizing that lives only in `run_wing_strategy` / engine config — `backtest_strategy.py` has no `--wing-flat-usd` / `--wing-fee-aware` flag, so the headline number was an inline analysis. Add the flags so it reproduces from one command. |
| cleanup | `data/hrrr_12z_KMDW_legacy.parquet` | 9-var pre-path-5 snapshot, deletable |

---

## 8. Constants

```
STATION              = KMDW
LOCAL_TZ             = America/Chicago
T_HOUR_LOCAL         = 13 (v3)
PEAK_WINDOW          = 12-16 local
HRRR_INIT_HOUR_UTC   = 12
HRRR_GRID            = (iy=665, ix=1169)
SEEDS                = [42, 43, 44]
QUANTILES            = 0.05..0.95 step 0.05 (19 total)
INTEGER_F_GRID       = arange(-30, 131)         (161 bins, 1°F wide)
KALSHI BUCKETS       = 4×2°F middles + 2 open-ended tails (per event)
USE_CALIB_FOR_SEASONS = {"JJA"}
HAS_HARD_FLOOR        = True (v3 only)
```

---

## 9. Key data files

### 9.1 Source / model data

| File | Contents | Window |
|---|---|---|
| `data/kalshi_history.parquet` | All KXHIGHCHI trades + quotes | 2026-03-21 → 2026-05-26 (67 events) |
| `data/cli_KMDW.parquet` | NWS CLI daily max truth | 2015-01-01 → present |
| `data/model_v3_artifacts/oof_bucket_probs_calib.npy` | OOF PMFs, shape (2332, 161) | 2021-2026 |
| `data/model_v3_artifacts/feature_df.parquet` | 46-feature snapshot per OOF row | |
| `data/model_v3_artifacts/target_df.parquet` | `cli_high` per date | |
| `data/model_v3_artifacts/final_models.joblib` | Deployable ensemble | last trained 2026-05-23 |
| `data/{paper,live}/{live_log.parquet, positions.json}` | Resident bots' per-mode trade log + Book — **pulled from the droplet** (`PullBotState`); **stay IN the repo** | growing |
| `..\data\weather\backfill\<SERIES>\<date>.zip` | **Truth tape** — deep trade tape + `settlement_value`, 20 cities; `load_city`'s source. **Moved out of the repo 2026-06-08**; repo `data/backfill` is now a **directory junction** to it (so `load_city`/backtests are unchanged). Kept current by `RefreshTruthTape`. | 2023-01-01 → present (~1,250 days/deep city) |
| `..\data\weather\orderbook\<SERIES>\<date>.zip` | Raw weather **market data** — forward bid/ask + depth ladders, all 20 series (`orderbook_logger.py` on the droplet → `PullOrderbookZips`). **Moved out of the repo 2026-06-08** (`OB_LOCAL_DIR`). | 2026-06 → growing |

> **Local data layout (2026-06-08):** the two bulky weather datasets live OUTSIDE the repo at
> `..\data\weather\{orderbook, backfill}` (raw market data + truth tape); bot logs stay in the repo
> (`data/{paper,live}`). All droplet→local pulls (`PullOrderbookZips`, `PullBotState`, `RefreshTruthTape`)
> follow a **zip / move / delete** convention (byte-verify the local copy, then delete the droplet copy) →
> the droplet keeps only the live Book + the in-progress day's raw snapshots. See `deploy/README.md`.

### 9.2 Cached backtest results

Each backtest run produces two parquets: `<tag>.parquet` is the daily summary
(`day_pnl_cents`, `n_targets`, etc.) and `<tag>_positions.parquet` is the per-leg
detail (`date`, `ticker`, `bucket_spec`, `contracts`, `stake_cents`, `fee_cents`,
`net_cents`, `won`, `p_model`, `p_market`, `kelly_f`, `bankroll_pre`, ...).

**Profitable variants — current snapshots:**

| # | Strategy | File stem |
|---|---|---|
| 1 | `wing + drop_lower_ask` (PRODUCTION) | `data/retest_wing_droplow` |
| 2 | `market_wing + drop_lower_ask` | `data/mw_droplow` |
| 3 | `wing_any + drop_lower_ask` | `data/retest_wingany_droplow` |
| 4 | `wing + drop_worst_leg` | `data/retest_wing_dropworst` |
| 5 | `wing` prob-weighted | `data/retest_wing_prob` |
| 6 | `wing` market-weighted (pow=5) | `data/retest_wing_mkt5` |
| 7 | `wing` eq-payout (loose, max=1.00) | `data/retest_wing_loose` |
| 8 | `wing` eq-payout (strict, max=0.97) | `data/retest_wing_strict` |

**Negative variants (kept for placebo / failure-mode reference):**

| Strategy | File stem |
|---|---|
| `joint_kelly` | `data/retest_joint_kelly` |
| `wing + drop_higher_ask` (PLACEBO) | `data/retest_wing_drophigh` |
| `wing` market-weighted (pow=3) | `data/retest_wing_mkt3` |
| `wing_any` (no agreement) | `data/retest_wingany` |
| `wing_any + drop_higher_ask` (PLACEBO) | `data/retest_wingany_drophigh` |
| `variance` | `data/retest_variance` |
| `hrrr_bias` | `data/retest_hrrr_bias` |
| `regime_confident` | `data/retest_regime_conf` |
| `market_wing` baseline (3-leg) | `data/mw_baseline` |
| `market_wing + drop_higher_ask` (PLACEBO) | `data/mw_drophigh` |
| `market_wing + drop_lower_ask` + agreement (== production) | `data/mw2_market_agree` |
| `wing + drop_lower_ask` (re-run for comparison) | `data/mw2_model_agree` |

Quick read:
```python
import pandas as pd
pos = pd.read_parquet("data/retest_wing_droplow_positions.parquet")
```

**Regenerate all:**
```powershell
python scripts/all_variants_retest.py          # 12 variants
python scripts/market_wing_test.py             # 3 market_wing variants
python scripts/market_wing_diag.py             # market_wing + agreement comparison
```

**Stale-cache risk:** these files are NOT regenerated automatically. Re-run if
the model is retrained, new Kalshi data is pulled, or any wing-strategy code
changes. The numbers in the §1 tables of this hand-off were computed from the
current snapshots.

---

## 10. Conventions

- Wing **anchor** = the bucket the wing is centered on. Default is `model_modal`. `--wing-anchor market` switches to `market_modal` (used by `market_wing` dispatcher).
- **Equal-payout sizing**: per-leg stake = K × yes_ask, where K = total_stake / sum_asks. After integer rounding, n_contracts ≈ K on every leg, so the gross payout on any winning leg = K dollars.
- **Agreement day** = day where model_modal_ticker == market_modal_ticker. Empirically 56% of days; 100% Top-3 hit rate at Kalshi resolution.
- The Kalshi day-event has six contracts (4 middle 2°F buckets + 2 open-ended tails), not five and not 3°F-wide. Modal-at-edge happens when model picks one of the two tails.
