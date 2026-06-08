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
(CLAUDE.md §4/§6). Untested-but-adjacent leads noted here: the **long-confirm** side of the ratchet
(buy the settling-in bucket if the book lags the lock-in — only the short side was tested), and
**favorite-longshot calibration** (price-band mispricing across the deep tape).
