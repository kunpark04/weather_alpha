# strat_prod_2 — research probe log

A research branch for model-free market-structure / microstructure edge exploration on Kalshi
daily-high temp markets. Read-only feasibility probes; **no v3 model, no weather as a predictor**
(weather obs used only as settlement/kill truth). Findings are mechanics reads, then extended toward
statistical verdicts as data accumulates.

---

## Probe 1 — MM (market-making) spread capture · CONFIRMED DEAD (N=6)

`scripts/mm_feasibility_probe.py` on the intraday L2 orderbook (`data/orderbook/<SERIES>/<date>/*.jsonl`).

- **2026-06-02 (N=1):** spreads at the 1¢ tick floor on CHI; even with the 75% maker discount, net
  capture razor-thin and erased by adverse selection. Not viable.
- **2026-06-08 extension (N=6, Jun 1–6 CHI):** holds + strengthens. On the **liquid, two-sided**
  buckets (high contested-snap count + flow — the ones you could actually fill), median spread is
  1–3¢ and `NET_c` (spread − round-trip fee, *before* adverse selection) is **−2.4 to +0.7¢** — i.e.
  negative-to-zero every active bucket, every day. The only positive-`NET_c` buckets are illiquid
  tails with ~0 flow (uncapturable mirages). **Verdict: no spread to capture where it's tradeable.**

## Probe 2 — running-max ratchet (short dead buckets) · CONFIRMED DEAD (N=6)

`scripts/ratchet_probe.py`: time-aligns running-max temp obs → per-bucket KILL time vs the book's
repricing-to-dead. Positive `mkt-obs` lag = book lagged obs (harvestable); ≤0 = anticipated (no edge).

- **2026-06-02 (N=1):** the book ANTICIPATED the realized running max (CHI led obs by ~117 min). No lag.
- **2026-06-08 extension (N=6):** on every **genuine afternoon heat-kill**, `mkt-obs` is negative or
  zero — Jun 2 (−122, −180), Jun 3 (0, −118), Jun 4 (−64), Jun 6 (−84, +1, +2, −542). The market is
  dead before/with the obs. The only "EDGE" flags were artifacts: one sub-obs-granularity (+2 min),
  and one **UTC-vs-local-day bug** (a prior-evening 82°F at 00:53Z killing the `<=81` bucket on the
  wrong day → fake 75¢ harvest). **Verdict: the book is forward-looking; no harvestable ratchet lag.**

### Tooling added this session (2026-06-08)
- `fetch_obs_iem()` — IEM-ASOS historical-obs fallback (weather.gov only serves ~recent days; IEM
  covers days that have aged out, lag ~24–48h). Caveat: IEM serves hourly METAR (~24/day) vs
  weather.gov specials (~100/day) — coarser for minute-scale lag, but the anticipation direction is
  unambiguous.
- **Day-boundary fix** — running max now over the **LOCAL (CT) calendar day** (`STATION_TZ`), not the
  UTC day; removes the spurious overnight kills (the false Jun-5 EDGE is gone; real kills unchanged).

---

## Conclusion + repoint

Both original directions (MM spread capture, running-max ratchet short) are **confirmed dead on N=6**,
not just N=1 — re-validating CLAUDE.md #2 (the market is forward-looking and prices the forecast).

**Repointed (2026-06-08):** test **batches of NEW model-free ideas** via
`scripts/idea_batch_screen.py` — screen many market-data-only hypotheses on the deep tape
(`load_city`, 3.4 yr × 20 cities) with IS/OOS + placebo discipline, respecting the graveyard
(CLAUDE.md §4/§6). An inventory of the ~60 existing scripts confirmed which axes are already covered
(sum-gate sweeps, sizing-by-sum, anchor-hour grids, seasonality-by-month, maker) — batch #1 targets
genuine gaps.

### Repoint batch #1 — model-free screens (2026-06-08) · NO new edge

- **A. Favorite-longshot calibration** (realized win rate vs market-implied last-trade price, per
  contract, 45k obs): market well-calibrated at low/mid prices (|gap| ≤ 1.3¢ for 0–45¢). **Favorites
  mildly OVERpriced** — 45–85¢ gap −2 to −4.4¢ IS, persisting OOS ~−1 to −2.8¢; extreme favorites
  (85–100¢) overpriced IS (−6.5¢) but **does NOT persist OOS** (+0.2). Every persistent gap is ≤ the
  spread+fee (~2–3¢) → not tradeable as a taker, and maker is dead ([[L15]]). Real tilt, sub-cost.
- **B. Day-of-week** (Chicago wing edge by weekday): **noise** — IS and OOS weekday rankings are
  uncorrelated (IS best Fri/Sat +5–6¢; OOS best Wed/Mon +16–19¢ on ~16-fire cells). No stable effect.
- **C. Interior-vs-tail wing** (legs both interior vs touching a tail): **no differentiation** — both
  ~+5¢ OOS at ~80% WR (the IS +8.6¢ tail edge evaporates OOS). Wing edge is uniform across composition.

**Batch #1 verdict:** no cost-surviving, OOS-robust model-free edge on these axes — the market is
efficient here (consistent with the project prior; re-validates CLAUDE.md #2). Only real structural
finding: a sub-cost favorite-overpricing tilt (45–85¢).

### Repoint batch #2 — model-free screens (2026-06-08) · NO new edge

`scripts/idea_batch2_screen.py` (reuses batch-#1 helpers), deep tape, IS/OOS 80/20.

- **D. Cross-city confidence synchrony** (Chicago wing edge by the other 19 cities' mean modal
  confidence, terciles): high-synchrony days lift Chicago WR (86% vs 77% low) but edge is **≈ baseline**
  (high-tercile OOS +5.4¢ vs the wing's ~+5¢), the IS/OOS tercile rankings **disagree** (IS best=high,
  OOS best=low), and the signal is redundant with Chicago's own confidence (the existing sum-gate
  already captures "confident days"). Not an additive spatial edge; gating cuts fires to ⅓ for no lift.
- **E. Long-confirm** (back the single market-modal bucket by anchor hour 11–16): **−EV at every hour**,
  IS and OOS. Win% climbs 55→82% as the running max locks in (real info), but the ask climbs faster
  (0.60→0.80) — you pay up for the certainty (favorites overpriced, batch-#1 A). The slight late-hour
  rise toward 0 is a stale-price artifact ([[L14]]) and is negative OOS. The ratchet LONG side is as
  dead as the short side.

**Combined batches #1+#2 — 5/5 model-free ideas dead** (calibration, day-of-week, interior/tail,
cross-city synchrony, long-confirm). The model-free edge space around the production wing is
**efficient**; no easy refinement or new market-data-only edge exists. The Chicago coverage wing
(~+4.6¢ realistic OOS, borderline) remains the only edge. Re-validates CLAUDE.md #2 repeatedly.
Recommendation: pause model-free probing (diminishing returns); the highest-value work is confirming
the production wing FORWARD (the `FWD-TRACK` tracker), not mining more market-data-only variants.
