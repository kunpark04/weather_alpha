# Late-night directional probe — top-1/top-2 coverage, cost & edge (2026-06-15)

**Thesis tested (user's):** late at night the daily temp extremum is realized, so most daily
high/low markets are "essentially settled" (~98–99% in a bucket); occasionally a favorite is still
resting at ~85% that "hasn't moved yet," so buying it earns a high return-for-time as it converges
to $1. Measure **top-1 / top-2 coverage** at **8 PM → midnight local, 30-min steps** for each city,
for **high- and low-temp** markets.

> **Verdict: the coverage premise is true, but the trade is not.** There is **no executable
> late-night edge** in either market.
> - **High-temp:** by 8 PM the favorite is already priced at **~0.99–1.00** with **~100% coverage**
>   (current regime). The favorite's real **ask is 1.00 on 99.2%** of overnight snapshots — you pay
>   full price. The "cheap stale favorite" is a ~0.6% rarity. The +2–4% "edge" that shows up on a
>   *last-trade* basis is a **staleness artifact** (thin overnight tape carries forward an afternoon
>   print) concentrated in **2023–24**; it has **decayed to ~0** and the ask never supported it.
> - **Low-temp:** coverage **rises 87%→93% through the night** because the daily *minimum is still
>   forming* late evening — so the cheaper favorite (~0.88) reflects **genuine, accurately-priced
>   residual uncertainty**, not a stale mispricing. Net-of-fee edge is **~0 to −2%** on last-trade
>   (and worse on the ask, which we can't yet see for low-temp).

---

## 1. What was measured

- **Snapshots:** 20:00, 20:30, 21:00, 21:30, 22:00, 22:30, 23:00, 23:30, 00:00 **local** (each
  city's tz; 00:00 = midnight ending the event day). DST-correct via `zoneinfo`.
- **Ranking / favorite:** at each snapshot, each bucket's price = its **last trade ≤ snapshot**
  (carry-forward — exactly the "resting/stale" price the thesis is about). Favorite = highest-priced
  bucket; top-2 = the two highest. (Orderbook cross-check ranks by bid/ask **mid**.)
- **Coverage:** P(the bucket that settled `yes` is the top-1 / is within the top-2). Truth =
  Kalshi's own `settlement_value` (exactly one `yes` bucket per event). No station/CLI basis risk.
- **Cost & edge (the half coverage alone hides):**
  - `price1` / `sum2` = favorite price / 2-leg cover cost.
  - `gap1 = top1 − price1` (gross edge per $1 on the 1-leg favorite buy); `net1 = gap1 − fee`
    (canonical Kalshi taker fee `ceil(7%·P·(1−P))`, `weather_alpha/fees.py`); `gap2 = top2 − sum2`.
  - Coverage **alone is not tradeable**: if the price already equals the win rate, edge = 0
    (`tasks/lessons.md` L15 / CLAUDE.md lesson #13 / `topk_by_hour_allcities` note).

### Data sources
| Source | What | Span / N | Use |
|---|---|---|---|
| Deep backfill (`scripts/backfill_cities.py`) | per-trade history, settlement truth | **2023-01-01 → 2026-06-13**, 20 high cities (~1,259 days each) | high-temp coverage + last-trade cost |
| Orderbook tape (`scripts/orderbook_logger.py`) | 60-s bid/ask/book snapshots | **2026-06-01 → 06-13**, ~10–13 days × 20 high cities | high-temp **real-ask** cross-check |
| Low-temp backfill (`scripts/backfill_lowtemp.py`, **new, keyless**) | per-trade history, settlement truth | **~2026-04-09 → 06-13**, ~67 traded days × 19 low cities | low-temp coverage + last-trade cost |

**Low-temp data did not exist before this probe.** Kalshi's `KXLOWT*` series are new and only
became liquid ~April 2026 (older settled events have zero trades, so ~67 traded days per city). NYC
has no low-temp series. **No orderbook tape exists for low-temp** → low-temp cost is last-trade only
(an *upper bound* on edge; the real ask is higher).

---

## 2. High-temp results

### 2a. Coverage (the user's direct question) — robust, large N
Daily high is realized by mid-afternoon, so by 8 PM the market is settled. Coverage is already at
its plateau across the whole 8 PM→midnight window (it barely moves).

**Pooled, full 3.4 yr (N≈8,160/snapshot):** top-1 **96–97%**, top-2 **99%**, favorite last-trade ~0.93.
**Pooled, current regime (Apr–Jun 2026, N≈1,478/snapshot):** top-1 **100%**, top-2 **100%**, favorite **0.99**.

### 2b. Edge — not executable (three independent confirmations)
1. **Recent last-trade:** every snapshot 20:00→00:00 → `price1≈0.99`, coverage ~100%, **net1 ≈ 0%**.
   Per-city: **0 of 20 cities** clear +3% net; all within ±1%.
2. **Real ask (orderbook tape, 20 cities, N≈190/snapshot):** favorite **ask = 1.00 on 99.2%** of
   snapshots (median 1.00; only 0.6% below 0.99; min 0.82). `gap1 = +0%`, `net1 = +0%`. You pay full
   price; the favorite is *not* a free convergence.
3. **The last-trade "edge" is a staleness artifact + edge decay:** the full-3.4 yr `price1≈0.90`
   (gap +3–4%) is dominated by **2023–24**, when overnight prints were stale-low and the market
   under-traded. In the recent 400 days the favorite's last trade is a **fresh ~0.98** (median print
   age ~30 min) with coverage ~0.99 → gap ~0. The old "edge" cities (Chicago/Miami/Austin/NYC, +3–4%
   on last-trade) collapse to net ~0 in the current regime, and the ask never supported them anyway.

> High-temp confirms the premise ("settled by night") and refutes the trade ("cheap favorite"):
> the price has already risen to match the ~100% win rate. This is exactly CLAUDE.md lesson #13.

---

## 3. Low-temp results (final, 19 cities, ~67 traded days each)

**Pooled (N≈1,260/snapshot):**

| snap | top-1 | top-2 | price1 | gap1 | net1 | gap2 |
|---|--:|--:|--:|--:|--:|--:|
| 20:00 | 89% | 96% | 0.89 | +0% | −1% | −3% |
| 22:00 | 91% | 97% | 0.90 | +1% | −0% | −2% |
| 23:30 | 94% | 98% | 0.92 | +2% | +1% | −2% |
| 00:00 | 95% | 99% | 0.93 | +2% | +1% | −2% |

**Interpretation:** unlike the high, the daily **minimum is still forming** in the late evening
(temperature still dropping toward the overnight low), so the market is **legitimately uncertain** at
8 PM and converges as midnight approaches — top-1 coverage **rises 89%→95%**, favorite price rises
0.89→0.93 in lockstep. The cheaper favorite is **correctly-priced residual risk**, not a stale quote:
the ~11% it sits below 100% materializes as real losses at roughly the right rate (gap ≈ 0). A
marginal last-trade **net +1% near midnight** appears, but it is on last-trade (the real ask, unseen
for low-temp, is higher) and within noise. The 2-leg cover is always negative.

### 3a. Per-city low-temp — top-1 coverage by snapshot (+ top-2 @ midnight)
| City | N | 20:00 | 21:00 | 22:00 | 23:00 | 00:00 | t2 00:00 |
|---|--:|--:|--:|--:|--:|--:|--:|
| Phoenix | 66 | 98% | 100% | 100% | 100% | 100% | 100% |
| Miami | 66 | 92% | 94% | 97% | 97% | 98% | 98% |
| Atlanta | 66 | 94% | 94% | 95% | 94% | 98% | 100% |
| Dallas | 67 | 96% | 94% | 93% | 93% | 99% | 100% |
| San Antonio | 67 | 86% | 89% | 88% | 94% | 99% | 100% |
| New Orleans | 66 | 92% | 92% | 91% | 91% | 98% | 98% |
| Austin | 67 | 91% | 94% | 94% | 96% | 97% | 100% |
| Houston | 67 | 91% | 94% | 93% | 94% | 96% | 99% |
| Los Angeles | 67 | 84% | 90% | 91% | 93% | 96% | 99% |
| San Francisco | 67 | 88% | 88% | 93% | 93% | 96% | 100% |
| Boston | 67 | 94% | 93% | 94% | 97% | 96% | 99% |
| Las Vegas | 67 | 94% | 96% | 94% | 96% | 94% | 99% |
| Minneapolis | 67 | 88% | 88% | 88% | 93% | 94% | 97% |
| Oklahoma City | 67 | 89% | 89% | 91% | 93% | 94% | 99% |
| Seattle | 67 | 91% | 91% | 91% | 93% | 93% | 100% |
| Chicago | 67 | 76% | 83% | 84% | 88% | 90% | 97% |
| Washington DC | 67 | 80% | 79% | 85% | 84% | 90% | 100% |
| Philadelphia | 66 | 82% | 82% | 83% | 85% | 88% | 100% |
| Denver | 67 | 84% | 85% | 87% | 85% | 87% | 91% |

Coverage is lowest at 8 PM for continental/variable-climate cities (Chicago 76%, DC 80%, Philadelphia
82%, Denver 84%) and highest/flat for desert/stable ones (Phoenix 98–100%) — consistent with the
overnight minimum still forming where late-evening cooling continues, and being locked early where it
doesn't. **Per-city midnight positives** (NOLA, Dallas, Atlanta, Houston at +3–6% net on last-trade)
are within N≈66 noise across 19×9 cells and unverified against the ask — see the adversarial review
(§4) before treating any as edge.

### 2c. Per-city high-temp (recent Apr–Jun) — top-1 coverage
Essentially **100% at every snapshot for every city** (lowest: Chicago 97%→100%, Houston 99%); all
favorites priced ~0.99 → net edge ~0. Fully settled and fully priced by 8 PM.

---

## 4. Adversarial review (self-performed)

A 4-lens multi-agent review was launched but died on a transient API 529 overload; the checks were
done inline instead. Significance uses H0: *true win-prob = price1* (the breakeven), one-sided,
normal approximation (exact-binomial numeric confirmation pending API recovery — does not change the
conclusion).

- **Significance / multiple testing (per-city low @ 00:00).** The midnight "positives" are not robust:
  New Orleans z≈1.91 (p≈0.03), Dallas/Atlanta z≈1.5 (p≈0.06), Houston/San Antonio p≈0.14. **None
  survive Bonferroni** (0.05/19 = 0.0026). Across all 19×9 = 171 cells, ~8–9 false positives at
  α=0.05 are *expected by chance*; the few observed are consistent with noise. ⇒ Claim 3 holds (noise).
- **Sign stability.** Dallas (+5%→+4%) and New Orleans (+4%→+6%) are positive at both 20:00 and 00:00
  (mild persistence), but Boston (+4%→0%) and San Antonio (−3%→+3%) flip — flips are the noise
  signature. Even the persistent two are small-N, single-season, last-trade.
- **Steelman (try to find a real edge).** Best candidate = NOLA/Dallas low-temp near midnight.
  Net of a plausible thin-market spread the last-trade gap collapses: NOLA +6% → **+3% (−3¢) / 0%
  (−6¢)**; Dallas +4% → **+1% / −1%**. With no low-temp ask data, single season, N≈66, and
  failed multiple-testing, **no defensible executable edge.** High-temp's full-history +3–4%
  (Chicago/Miami/Austin/NYC) is already dead in the current regime and never existed on the ask.
- **Methodology / leakage.** Logic correct by construction: tz/DST via `zoneinfo`, 00:00 = next-day
  midnight, exactly-one `yes` winner enforced, price = last trade *strictly ≤* snapshot (no
  look-ahead). **Internal control:** the same machinery yields flat ~100% for high-temp over the same
  snapshots, so the low-temp 89%→95% *rise* is a real low-temp property, not a method artifact — and
  `price1` rising in lockstep (0.89→0.93) confirms genuine convergence, not just more buckets getting
  priced through the night.

**Review verdict:** all three headline claims **hold**. The one item worth *watching* (not trading)
is the warm-Gulf-coast low-temp favorite near midnight — re-evaluate only with low-temp **ask** data
and a second season.

## 5. Caveats / what would change the verdict
- **Low-temp ask is unseen.** Cost is last-trade only; the real ask is higher, so low-temp edge is
  *at best* the ~0/−2% shown — almost certainly worse. To measure it, **start logging `KXLOWT*`
  orderbooks** (add them to `scripts/orderbook_logger.py SERIES`); a multi-week tape would settle it.
- **Low-temp N is young & seasonal** (~67 traded days, Apr–Jun only). N>20 so not a no-verdict, but
  no winter/fall coverage yet (lesson #4). The clean "min-still-forming" signature is robust to N.
- **High-temp real-ask is June-only** (13 days). The ask=1.00 result is uniform and corroborated by
  the recent last-trade and per-city checks, but multi-season ask data (the running logger will
  accumulate it) would make it airtight.
- **Last-trade ranking mixes bucket staleness** (different buckets' last trades have different ages);
  this adds noise to the favorite pick but the OB mid-ranked cross-check agrees.

## 6. Low-temp modal backtest — best local entry hour, $25 account (user-requested)

Strategy (user spec): at a fixed local hour ≥10 PM, for every city's low-temp event scan the
**largest modal bucket** (favorite); if its prob ≤ 90%, **buy it**, splitting the **whole $25
balance evenly** across all qualifying events that night; hold to settlement; compound. Sweep the
entry hour. Entry price = favorite last trade (no low-temp ask exists → optimistic). Window = the
~67 traded days × 19 cities (Apr–Jun 2026). `scripts/lowtemp_modal_backtest.py`.

| entry | days | events | win rate | avg px | netEV¹ | full-deploy $25² | wipeouts³ | de-risked 25%/day $25⁴ | maxDD |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 22:00 | 67 | 341 | 70.1% | 0.69 | −1.6% | **RUIN ($0)** | 1 | $20.43 (−18%) | −100% |
| 22:30 | 67 | 316 | 71.5% | 0.70 | +0.7% | **RUIN ($0)** | 1 | $15.03 (−40%) | −100% |
| 23:00 | 67 | 297 | 70.7% | 0.71 | −1.1% | **RUIN ($0)** | 2 | $25.78 (+3%) | −100% |
| **23:30** | 67 | 281 | 75.1% | 0.71 | **+6.2%** | **$102.41 (+310%)** | 2 | **$76.70 (+207%)** | −94% |
| 00:00 | 66 | 236 | 75.0% | 0.71 | +3.7% | **RUIN ($0)** | 4 | $30.19 (+21%) | −100% |

¹ sizing-free per-event EV (last-trade, fee-adj) — the real edge signal. ² literal spec: 100% of
balance split evenly, compounding. ³ # nights the account fell >50% (correlated low-temp losses
across cities). ⁴ deploy only 25% of balance/night (rest cash) — isolates edge from ruin.

**Verdict:** *as specified the strategy self-destructs.* Deploying 100% of the balance every night
means one bad night — and low-temp outcomes are **correlated across cities** (a single warm/cold
anomaly moves many at once, so "split across events" is not real diversification) — craters the whole
account; **4 of 5 entry hours go to $0**, and the lone survivor (23:30) only made it by luck with a
**−94% drawdown**. The "best hour" is **23:30** on every metric, but it is **not a robust edge**:
netEV +6.2% is ~1.7 SE from zero (not significant), it jumps implausibly from −1.1% at 23:00 to
+6.2% in 30 min (noise signature), and it is **last-trade** — the real low-temp ask (unquoted in our
data) is higher, very likely pushing it ≤0. Favorites here trade ~0.70 *because the daily low is
genuinely still forming at 10 PM–midnight* (§3): you are paid roughly fairly for real risk, not
picking off a stale mispricing. **Not viable.** If ever revisited: cap per-night deployment (the 25%
column shows ruin disappears but growth is luck-driven), and only after logging low-temp **asks**.

### 6a. Band variant — buy only when 85% ≤ favorite ≤ 95%, fixed 10 PM entry (user iteration)

Restricting to a band (skip the too-uncertain <85% and the already-converged >95%) **fixes the ruin
problem and turns netEV positive — on last trade.** Full hour sweep, $25:

| entry | days | events | win rate | avg px | netEV | full‑deploy $25 | wipeouts | de‑risked 25% | maxDD |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| **22:00 (10 PM)** | 62 | 240 | 95.0% | 0.92 | **+3.0%** | $82.83 (+231%) | 0 | $38.58 (+54%) | −72% |
| 22:30 | 66 | 213 | 93.9% | 0.92 | +1.9% | $248.17 (+893%) | 0 | $48.41 (+94%) | −34% |
| 23:00 | 62 | 212 | 92.9% | 0.92 | +0.8% | $59.35 (+137%) | 0 | $35.73 (+43%) | −66% |
| 23:30 | 64 | 210 | 92.4% | 0.92 | +0.1% | RUIN | 2 | $20.95 (−16%) | −100% |
| 00:00 | 63 | 198 | 95.5% | 0.92 | +3.5% | $176.87 (+608%) | 0 | $45.94 (+84%) | −53% |

**Per-city @ 10 PM** (band 85–95%): 14/19 cities netEV>0; 11 at 100% win rate on N=4–20.
Best Boston/DC/LV/LA/Chicago +9–10%; worst Seattle −15%, Minneapolis −10%, Denver −7%. Per-city
N is 4–20 → individual cells are noise.

**Verdict:** the band is the best-looking variant — at 10 PM, 95% win rate at avg last-trade 0.92,
pooled netEV **+3.0%**, the account grows, and single-night wipeouts drop to 0 (the band excludes the
cheap <85% favorites that caused correlated blow-ups). **But it is not yet a real edge:** (1) netEV
+3.0% is only **z≈1.93** (~p=0.05 one-sided) — marginal — and that's the *optimistic* last-trade
number; (2) it lives entirely in the **1–8¢ gap between last trade (0.92) and the real ask we cannot
see** — low-temp has no orderbook tape, and for the analogous high-temp market the favorite *ask was
pinned at 1.00* vs last ~0.99, which would erase this entire +3%; (3) single season, ~62 days; (4)
maxDD −72% even banded. ⇒ promising on paper, **execution-unverified**; decisive test = log low-temp
**asks** (add `KXLOWT*` to `scripts/orderbook_logger.py`) before risking money.

Reproduce: `python scripts/lowtemp_modal_backtest.py --bankroll 25 --band-lo 0.85 --thresh 0.95`
(artifact `data/lowtemp_modal_band_8595.parquet`).

### 6b. Further iterations — rolling entry, narrow bands, sizing & 50% cap (all converge to the same conclusion)

Subsequent tweaks explored entry timing, narrower bands, and deployment sizing. Sizing-free per-event
edge (netEV, last-trade) is the constant signal; the $-account differences are mostly sizing/path.

- **Rolling entry** (each city buys the first 11 PM→12 AM /15-min snapshot its favorite enters
  85–95%; `scripts/lowtemp_rolling_backtest.py`) with **balance ÷ available-markets** sizing:
  362/1268 city-days entered, win 92%, **netEV +0.2% (z=0.13)**, $25→**$24.75 (−1%)**, maxDD −34%,
  0 wipeouts. Rolling *rescues* the late-converging cities — exactly the unpredictable continental
  ones (Denver −29%, Minneapolis −17%, DC −13%) — which **cancels the edge**. Safe but flat.
- **Three narrow bands @ 11:30 PM, split across *traded* cities** (full deployment): 80–85% netEV
  +6.6% (z=1.29), 85–90% **−3.3%**, 90–95% +2.3% (z=1.33) — non-monotonic, none significant; **all
  three RUIN** (single-qualifier nights go all-in on one bet).
- **90–95% @ 11:30 PM, split across traded:** 160 trades, win 95.6%, avg 0.93, netEV +2.3% (z=1.33).
  Ruins on 2026-05-04 — **one city qualified, got 100% of the $75 balance, lost → $0.** Same band
  with **balance ÷ available** sizing → $30.20 (+21%, maxDD −9%).
- **90–95% @ 11:30 PM, 50% allocation cap:** removes ruin. Per-position cap (no bet >50%) →
  **$34.97 (+40%), maxDD −72%**; total-deploy ≤50%/night → **$20.30 (−19%), maxDD −65%**. Same 160
  trades, opposite outcome by sizing nuance ⇒ **path/luck-dominated**, not edge.

**Consolidated takeaway:** across every entry hour, band, and sizing, the low-temp "buy the favorite"
per-event edge is a **marginal, statistically-insignificant (|z|≤1.9), last-trade** signal. Aggressive
sizing (split across traded / full deploy) → ruin via single-qualifier all-in nights; safe sizing
(÷available, or ≤50% cap) → survives but lands anywhere from −19% to +40% on one 62-day season with
−65% to −72% drawdowns — i.e. a coin flip on noise. **No verified edge.** The single decision-relevant
gap is the **unobserved low-temp ask** (last-trade ≈0.92 but the real ask — by analogy to high-temp,
where it was pinned at 1.00 — likely erases the +2–3%). Build = log `KXLOWT*` orderbooks; stop tuning
band/hour/cap on last-trade. Artifacts: `data/lowtemp_modal_band_8595.parquet`,
`data/lowtemp_rolling_8595.parquet`. Scripts: `scripts/lowtemp_modal_backtest.py` (`--band-lo/--thresh`),
`scripts/lowtemp_rolling_backtest.py`.

## 6c. HIGH-temp AFTERNOON optimization — the one significant edge (4–6 PM, OOS)

Pivoted to **high-temp at 4–6 PM** (the high *locks in* mid-afternoon, so its market actually
converges then — unlike 8 PM-midnight where it's pinned ~1.00). Optimized band × stamp with an
**out-of-sample time split** (train < 2026 / test = 2026), a 2nd-favorite placebo, a real-ask
haircut, and a fundamental buy-and-exit variant. `scripts/hightemp_optimize.py`,
`scripts/hightemp_convergence.py` (caches `data/hightemp_aft_events.parquet`,
`data/hightemp_convergence.parquet`).

**Result — first edge to clear every check.** Strategy = buy the high-temp **favorite at 16:45–17:30
local when its price ∈ 90–95%**, split across traded cities, **hold to settlement**:
- OOS: train +3.2% (z=3.2) · test +3.6% (z=3.5); win rate 96–97%.
- **13/81 configs significant in BOTH periods** (chance ≈0.2 → p≈0; not data-snooping).
- **Placebo** (same rule on 2nd favorite) = −57.7% (z=−3.6) → edge is favorite-specific.
- **Breadth** 17/20 cities positive. **Survives the real ask**: +3.6% → **+2.3%** at the ~0.94 ask
  (96.9% win vs 94.7% breakeven). Ask measured on the 13-day tape (weakest link).
- **Band filter is load-bearing** (no band → +0.2%, z=0.3). **Buy-and-exit FAILS** — every
  sell-before-settle variant loses vs hold (market prices the exit efficiently; forfeits winner
  upside + double fee). Hold-to-settle is optimal.

**CLUSTER-ROBUST CORRECTION (the honest significance).** The z's above treat each city-event as
independent, but same-day cities are correlated → inflated. Re-tested with **date-clustered** z
(edge per day, t over days) AND at the real ask:
- 90-95 @ **17:30** (the naive "winner"): z_event 3.5 → **z_DAY 1.2** (last), **0.5 at ask** — NOT
  robustly significant OOS. Its strength was per-event-z inflation.
- 90-97 @ **16:45** (the truly robust config): z_event 3.7 → **z_DAY 3.3** (last) → **z_DAY 1.7 at the
  ask**, mean **+1.2%/$**. Sister bands 92-97 @ask z_DAY 1.9, 90-95 @ask z_DAY 1.4.
⇒ Done fully honestly (OOS + date-clustered + real ask), the edge is **+1.2-1.3%/event at z≈1.7-1.9
(~p=0.05) — marginal, not established.** Real but small; the impressive z=3.5 was last-trade +
independence inflation.

**$/day reality vs the $5/day goal.** $5/day on $25 = +20%/day; the edge is +2.3%/event and there is
~one (correlated) settlement/day → Kelly ceiling ~+2–3%/day, realized **~0.1–0.4%/day** at the ask
with non-ruinous sizing and **−47% drawdowns** (full deployment ruins; 50%/night loses at the ask).
Reaching the account size where ~$5/day occurs (~$1,667) takes **~3–11 years** of uninterrupted
compounding if the edge persists. **$5/day quickly is not attainable from a real edge** — only via
leverage Kalshi lacks or sizing that guarantees ruin. Verdict: a genuine, thin, significant edge
worth paper-trading at conservative sizing; **not** a fast-money strategy. Binding uncertainty =
the 13-day ask sample → accumulate high-temp 4–6 PM order-book asks before risking money.

### 6d. Sizing for growth — the honest path to "$5/day from $25"

Kelly-sized the validated edge (90-97 @ 16:45, hold, **at the ask**) on the **live 20-city regime**
(2026, 156 days, ~5.2 qualifiers/night → daily σ 8.9% vs 16.5% in sparse early history). Daily mean
+1.20%/$ deployed. Growth-optimal sizing + time for $25 to compound to where it yields ~$5/day:

| sizing | deploy/night | geo/day | $/day on $25 now | time→$5/day | maxDD |
|---|--:|--:|--:|--:|--:|
| full Kelly | 92% | +0.66% | $0.16 | ~1.4 yr | −61% |
| **half Kelly** | 46% | +0.46% | $0.12 | **~2.3 yr** | −30% |
| quarter Kelly | 23% | +0.25% | $0.06 | ~4.7 yr | −15% |

Half-Kelly ≈ +217%/yr ($25→$79 in 1yr→$251 in 2yr). **$5/day on $25 day-one (=+20%/day) is
impossible**, but **"$5/day that grows with the account" is reachable on a ~2–3 yr horizon** by
compounding at half-Kelly — IF the edge holds. Binding caveats: (1) ask-adjusted cluster-robust
significance is **marginal (z≈1.7, ~p=0.05)** — real chance it's partly noise; (2) the ask haircut is
from a **13-day** tape; (3) maxDD −30% (half-K) / −61% (full-K) is brutal on a tiny account; (4) the
2026-only regime could decay (the overnight high-temp edge did across years). ⇒ Forward **paper-trade
16:45 / 90-97% at half-Kelly** to validate against live asks before risking the real $25.

### 6e. Full optimization sweep (sizing / filters / seasonality / structure) — corrections & the one real improvement

4-lens parallel workflow (`hightemp-strategy-optimize`), each OOS (train<2026 / test 2026) + date-
clustered z + at-ask. Net: most of the search space is refuted, **one correction**, and **one fragile
improvement**.

- **CORRECTION to §6d:** the "+0.46%/day half-Kelly" was the **TEST-oracle** Kelly (f fit on the test
  series). **Honest train-chosen Kelly → +0.022%/day (~20× lower).** Prior $500→$1,022/156d and $5/day
  timelines were over-optimistic. Forward growth is far smaller and **liquidity-capped (~$5–13/night,
  binds almost immediately)** regardless of sizing. z_DAY is sizing-invariant (1.71 for all f).
- **Sizing rules:** none beat baseline OOS (Kelly/vol-target/DD-throttle/confidence all rejected;
  vol-target's +1%/day was a warmup artifact; fixed-fraction Sharpe degenerate at 0.137).
- **Filters:** none robust. City/DOW z=8–15 are WR=100% variance-floor artifacts (binomial p=0.06–0.43);
  **13/20 cities are test-only** (OOS split is a city-composition break); gap≥0.02 dies under Bonferroni-19.
- **Structure:** **multi-stamp pooling REFUTED** (lowers z_DAY — zero independent days added). Stamp axis
  a fragile cliff (TRAIN→TEST rank-corr 0.18). Edge 79% concentrated in Austin/Phoenix/Las Vegas; 12/20
  cities positive OOS.
- **SEASONALITY (key):** the validated z=1.71 is carried by **April 2026 alone (z=8.49)**; Jan/Feb/May/Jun
  sub-0.6 or negative; **December net-negative 3/3 years.** Edge is a spring/fall warm-stable-highs effect,
  NOT year-round. The one robust optimization = **drop_DJF (skip winter):** TEST z_DAY 2.76 (vs 1.71),
  netEV +1.9%, maxDD −4.4%; survives bootstrap (P≤0=0.005) + Bonferroni-7 (p=0.020). Fragile: gain partly
  mechanical (Dec absent from Jan–Jun test), fails full ~100-variant multiplicity, paired-day-indistinct.
- **BINDING RISK = the ask haircut** (verified): baseline netEV/z = +2.4%/3.37 at 0¢ → +1.2%/1.71 at the
  assumed +1.2¢ → +0.0%/0.06 at +2.4¢ (dead) → negative at +3.2¢. Whole edge in a ~1.2¢ window from a
  **13-day** tape. A sub-penny error kills every variant.

**Final verdict:** optimized = 16:45 / band [0.90,0.97] (optional tighten [0.92,0.95]) / **skip DJF** /
structural ruin-cap sizing. Real but fragile OOS edge (z_DAY 2.76 vs 1.71), April-concentrated, one
slippage-tick from zero, ceiling ~$5–13/night. **No growing-$5/day path.** Highest-value next step is
**more orderbook ask data**, not more strategy search. (Lens audits under `tasks/_agent_bus/20260615-0910/`.)

## 6f. Multi-city confidence algo — full testing log + LOCKED CONFIGS (2026-06-15)

Evolution of the §6 directional idea into a tunable algo: *buy the high-confidence favorite, emit a
calibrated confidence, diversify across cities, bet 50%* — tuned separately for low-temp (overnight)
and high-temp (afternoon). Scripts: `scripts/lowtemp_confidence_algo.py`, `lowtemp_2city_algo.py`,
`lowtemp_anchor_sweep.py`, `hightemp_optimize.py`, `hightemp_aft_events.parquet`.

**Algo mechanics.** At the anchor time, per city find the favorite (largest modal bucket) + its
last-trade price (= market P(settle there)). Gate to confidence ∈ [0.93, 0.95]. Select the top-N
cities by confidence; bet 50% of the account split evenly (16.7% each for 3); hold to settlement;
compound. Confidence = history-calibrated P(win).

**Testing log (all $250 start, last-trade prices = optimistic ceiling):**
- **Confidence gate.** ≥0.93 → ~97.5% hit; *relaxing* below 0.93 drops hit to ~92% and adds
  all-miss nights → ruin; *raising* to ≥0.96 (near-settled) → ~98.7% hit but ~0 edge (priced fairly).
- **Calibration check.** Stated confidence 0.974 vs realized 0.975 — essentially perfect (Brier 0.025).
- **Confidence-interval work.** low-temp-only hit-rate CI [91%,99%] (N=79); pooled with
  consistency-verified high-temp (two-prop z=−0.55) → **[96%,98%]**; near-settled ≥0.96 gate →
  **[98%,99%]** (but no return). [97,100] needs a true ≥99% rate or ~750+ picks (~1 yr more tape).
- **1 vs 2 vs 3 cities @ fixed 50% deployment.** 1-city risks −50% single-miss nights (high-temp:
  **−68% DD, 3 blow-up nights**); 2–3 cities split the 50% → **−24% DD, 0 all-miss, higher return**;
  3-city edges 2-city. Diversification raises return *and* cuts DD.
- **Sizing.** Full 50% (−24% DD, max return) vs 16.7%/city throttle (−16% DD, ~half return).
- **Anchor sweep — LOW-temp (8 PM–2 AM).** Hit ~98% robust at every anchor; the $-variation across
  anchors is **noise** on a 53–57-night sample (1–2 misses drive it). Chose **22:00**.
- **Anchor sweep — HIGH-temp (afternoon, N≈123).** **17:00 clearly best** (98.4% hit, 0 all-miss,
  −24% DD) and OOS-corroborated; 16:00 and 17:30–18:00 **blow up** (−68% to −97% DD, 5–6 all-miss)
  because the high isn't locked yet (16:00) or the band adversely selects still-contested days (late).

**LOCKED SUGGESTED CONFIGS** (top-3, 50% split, band 0.93–0.95; backtest = $250 start):

| param | LOW-temp | HIGH-temp |
|---|---|---|
| market | Kalshi daily LOW (`KXLOWT*`) | daily HIGH (`KXHIGH*`) |
| anchor (local) | **22:00** | **17:00** |
| confidence band | favorite price 0.93–0.95 | 0.93–0.95 |
| selection | top 3 by confidence | top 3 by confidence |
| sizing | 50% of acct split evenly (≤16.7%/city) | same |
| backtest window | ~56 nights (Apr–Jun 2026) | ~123 nights (2026) |
| hit rate | ~99% (in-sample) | 98.4% |
| $250 → end | ~$988 (**+$738**) | ~$2,644 (**+$2,394**) |
| ~$/day | ~$13 | ~$19 |
| max DD | −15% | −24% |
| calib. confidence | ~0.97 (CI [96%,98%]) | ~0.97 (CI [96%,98%]) |
| lower-DD variant | 16.7%/city → −15% DD | 16.7%/city → −16% DD, +$976 |

**CAVEATS (govern all numbers — these are NOT live-validated):** all figures use **last-trade**
prices → optimistic ceiling. The real **ask** cuts the edge — low-temp has **no ask tape at all**;
high-temp's 13-day tape shows the edge survives only **marginally (cluster-robust z≈1.7 at the ask)**.
**Liquidity** caps realized $/day in the **low tens** at size (best-ask depth ~$13–50/city), regardless
of the compounding endpoints above. Low-temp rests on **56 nights, one season**. Before risking real
money: **paper-trade both locked configs against live asks** (the logger already captures high-temp
4–6 PM; low-temp `KXLOWT*` order-book logging would need to be turned on).

## 6g. DEPLOYED forward paper-test (2026-06-15)

The two locked configs are now forward paper-testing against **live asks + order-book depth** on the
droplet (the `market_wing` live + paper bots were retired first — see CLAUDE.md §1).

- **Logger extended** to 39 series (20 `KXHIGH*` + 19 `KXLOWT*`) @ 60s — low-temp depth was captured
  nowhere before; now it is (tested: 234 mkts/cycle in ~13s, authed tier, 0 rate-limits). Unit:
  `deploy/orderbook-logger-user.service` (updated on the droplet + in-repo).
- **Scorer** `scripts/directional_paper.py`: `--capture` reads each city's anchor-moment book
  (high@17:00 / low@22:00 local), picks the favorite (mid ∈ 0.93–0.95), top-3, 50% split, and logs the
  **live `yes_ask`, full depth ladder, intended vs fillable contracts, VWAP/slippage walking the book
  to 0.97, and fillable-%** to `data/directional_paper/<market>_log.parquet`; `--settle` books paper
  P&L at the achievable fill via Kalshi settlement. Local validation (high-temp June tape): correctly
  flagged a 5%-fillable / $6-depth pick vs a 100%-fillable / $225-depth pick (avg fillable 52%).
- **Schedule:** two daily `systemd --user` one-shot timers on the droplet —
  `directional-paper-high.timer` (~00:30 UTC, post 17:00-PT) and `directional-paper-low.timer`
  (~06:00 UTC, post 22:00-PT). First captures fire 2026-06-16; scored P&L follows ~1 day after each
  settles. Logs live on the droplet (`data/directional_paper/`); pull/monitor via SSH (a local pull
  task can be added like the orderbook/state pulls). This is the live-ask validation the §6 caveats
  called for — it directly measures whether the ~0.93–0.95 last-trade edge survives the real ask and
  whether the intended stake is fillable against actual depth.

## 6h. Execution model — per-city-local anchor (A) vs single-UTC (B), same rule

To check whether the per-anchor design matters, the identical selection rule (top-3, band 0.93–0.95,
50% split, $250, compound) was run two ways, isolating only *when* each city's price is read
(`scripts/exec_model_backtest.py`). **Method A** reads each city at its own 17:00/22:00 LOCAL anchor;
**Method B** reads every city at one fixed UTC instant (swept — high {21,22,23,00} UTC, low {02–06} UTC,
deliberately different because the local anchors are 5 h apart).

| market | A: per-anchor | best B (single-UTC) | other B's |
|---|---|---|---|
| HIGH | **+$2,394, −24% DD, 98.4% hit, 0 all-miss** | +$1,825 @ 22:00 UTC (=17:00 CT) but −50% DD, −$1,599 worst night | 21/23/00 UTC ≈ flat/negative, −64% to −80% DD, 2–4 all-miss nights |
| LOW | **+$738, −15% DD, 99.1% hit, 0 all-miss** | +$916 @ 02:00 UTC (=22:00 ET), −15% DD | 03–06 UTC worse, −30% to −60% DD |

**Per-anchor (A) wins decisively** — highest return, lowest drawdown, zero all-miss nights. Single-UTC
(B) is unstable: it only approaches A at the one UTC instant that coincides with a major tz's anchor,
and even then carries far deeper drawdowns; at every other instant it trades cities at the wrong point
in their local convergence (some not yet in-band, some converged past it) → flat/negative with brutal
drawdowns. This confirms the staleness intuition and **locks Option A (per-anchor) as the model**.
Caveat: last-trade prices (both methods, fair comparison); the run used the look-ahead top-3 rule for
both to isolate timing — the live/executable rule (below) replaces it.

## 6i. Engineering — Rust LIVE engine + per-anchor Python PAPER shadow (2026-06-15)

The directional engine was rebuilt for **Option A true live replication**, split into a live path and
a paper shadow that share one **executable rule** (no cross-city look-ahead): favorite mid ∈
[0.93,0.95] → buy; **daily cap 3 entries/market/event-date, first-come as anchors fire east→west**;
each entry = `stake_fraction/cap` = **16.67%** slot; P&L at the achievable fill, net of fee.

- **LIVE = Rust** (`rust/`, see `rust/README.md`): a modular cargo workspace (`wa-fees/book/algo/
  kalshi/schedule/state/exec/engine`), 31 parity tests vs the Python, clippy-clean, live-smoke-tested
  (keyless fetch → favorite → band → feasibility → log). Resident daemon wakes at each city's local
  anchor, fetches the live book, decides, and is real-order-capable (RSA-PSS auth, limit-at-ask taker)
  but **ships built + UNARMED** (`deploy/wa-engine.service`, `deploy/build-engine.{sh,ps1}`). Built as
  a Linux GNU binary in WSL (droplet glibc 2.39 matches); musl-static is a one-line swap after
  `apt install musl-tools`.
- **PAPER = Python** (`scripts/directional_paper.py`, rebuilt): fetches the **LIVE** Kalshi book at each
  anchor (not the logged tape — staleness now ≈0), applies the same executable rule, paper-records with
  depth-feasibility, settles keyless. Fired **per (tz, market)** by 11 `systemd --user` timers
  `weather-alpha-paper-cap@*` (high 17:00 / low 22:00 local, ET/CT/MT/PT/AZ + a daily settle), via
  `scripts/paper_capture.sh` + `deploy/install-paper-timers.sh`. The old batch timers
  `directional-paper-{high,low}` (00:30/06:00 UTC) are retired. Deployed + validated on the droplet
  2026-06-15; first real captures at tonight's anchors. Note: the executable first-3 rule deploys ≤50%
  on thin nights and favors east-tz cities (they fire first) — the honest cost of no look-ahead vs the
  §6f backtest's cross-city top-3.

## 6j. Band-width decision — keep [0.93,0.95], reject [0.90,0.95] (2026-06-15)

Tested whether the entry gate is too narrow (`scripts/band_sweep.py`, both markets, 2026 per-city-anchor
data, fee-aware, the deployed top-3 / 50%-split / compound rule). **70% of favorites are already ≥0.95**
(excluded as fully-priced), so widening to [0.90,0.95] only ADDS the marginal [0.90,0.93) sub-band.

| market | band | entries | hit | PnL | maxDD | $/day |
|---|---|--:|--:|--:|--:|--:|
| HIGH | [0.93,0.95] | 250 | 98.4% | +$2,394 | −24% | +19.5 |
| HIGH | [0.90,0.95] | 346 | 97.4% | +$2,337 | −50% | +16.2 |
| LOW | [0.93,0.95] | 111 | 99.1% | +$738 | −15% | +13.2 |
| LOW | [0.90,0.95] | 138 | 97.8% | +$483 | −51% | +3.9 |

Marginal [0.90,0.93): HIGH n=168, hit 94.6% @ price 0.911; LOW n=49, hit 89.8% @ 0.911.

**Decision: KEEP [0.93,0.95].** Adversarially verified (stats-ml-logic-reviewer + an independent recompute
— all numbers reproduced exactly). Widening is rejected because: (1) the HIGH marginal "+EV" is **not
robust** — per-entry CIs overstate significance (entries cluster by day); day-clustered **z_DAY≈0.83
(p≈0.20)** with a win-rate lower bound below break-even, before the ask haircut (which hits these thin
sub-0.93 favorites hardest). (2) LOW [0.90,0.93) is **−EV** (wins less than its price). (3) The widened-band
PnL is **counterfactual** — the −50%/−51% max-DD comes from single nights where 50% rides one thin
0.90-favorite; those days latch the account drawdown halt (which the sim doesn't model), freezing trading,
and the narrow band excludes exactly those nights. The band is the risk-adjusted sweet spot, not too narrow.
The higher-leverage fix is **sizing** (cap per-leg exposure / deploy the full 50% only when ≥2 cities
qualify), not the gate. See `tasks/lessons.md` L22.

## 7. Reproduce
```
python scripts/band_sweep.py                                  # §6j band [0.93,0.95] vs [0.90,0.95]
python scripts/exec_model_backtest.py --rebuild               # §6h per-anchor (A) vs single-UTC (B)
python scripts/backfill_lowtemp.py --days 400                 # one-time low-temp pull (keyless)
python scripts/late_night_coverage.py --kind high             # high, full 3.4 yr
python scripts/late_night_coverage.py --kind high --since 2026-04-01   # high, current regime
python scripts/late_night_coverage.py --kind low              # low
python scripts/late_night_coverage_ob.py                      # high real-ask cross-check
python scripts/lowtemp_modal_backtest.py --bankroll 25 --thresh 0.90   # §6 modal backtest
python scripts/hightemp_optimize.py --rebuild                 # §6c OOS band×stamp optimization
python scripts/hightemp_convergence.py --rebuild              # §6c buy-and-exit variant (fails)
python scripts/lowtemp_confidence_algo.py --tau 0.93          # §6f single-pick confidence + calibration
python scripts/lowtemp_2city_algo.py --tau 0.93               # §6f 2-uncorrelated-city + calibration
python scripts/lowtemp_anchor_sweep.py --rebuild              # §6f low-temp anchor sweep (-> 22:00)
python scripts/directional_preset.py --preset hightemp17      # §6f LOCKED preset: high-temp @17:00
python scripts/directional_preset.py --preset lowtemp22       # §6f LOCKED preset: low-temp @22:00
cargo test --manifest-path rust/Cargo.toml                    # §6i Rust LIVE engine parity tests (31)
cargo run --manifest-path rust/Cargo.toml -p wa-engine -- --once --dry-run   # §6i live smoke (no orders)
powershell -File deploy/build-engine.ps1                       # §6i build the Rust Linux binary (WSL)
```
Runnable presets live in `scripts/directional_preset.py` (`--preset {lowtemp22,hightemp17}`,
`--bankroll`, `--throttle` for the 16.7%/city low-DD variant). Backtest/sim only — not live-wired.
Artifacts: `data/late_night_coverage_high.parquet`, `data/late_night_high_recent.parquet`,
`data/late_night_low.parquet`, `data/late_night_ob.parquet`, `data/lowtemp_modal_backtest.parquet`.
