"""Band comparison for the directional algo: [0.93,0.95] vs [0.90,0.95], both markets.

Uses the per-city-local-anchor favorite price+win (data/exec_model_events.parquet, eval='anchor' =
the deployed Option-A model's data). Three views:

  1. CAPPED STRATEGY (the deployed rule): per date, favorites with price in band -> top-NC by price,
     50% split into NC slots, compound from $250, fee-aware. Same engine as exec_model/§6f/§6h.
  2. PER-ENTRY quality: every in-band favorite at a flat view -> hit% vs mean price.
  3. MARGINAL sub-band [0.90,0.93): the entries widening would ADD. Realized hit% vs price+fee +
     Wilson 95% CI + a temporal-stability split. This is the decision driver: per CLAUDE.md lesson
     #13 the price already encodes the win rate, so widening only helps if these cheaper favorites
     are mispriced UP by more than the fee.

Last-trade prices (the real ask is >= these, so any -EV here is worse live, esp. for thinner
sub-0.93 favorites). 2026 only (the exec_model cache). Usage: python scripts/band_sweep.py
"""
from __future__ import annotations
import math
from pathlib import Path
import numpy as np
import pandas as pd

CACHE = Path(__file__).resolve().parent.parent / "data" / "exec_model_events.parquet"
NC = 3
STAKE = 0.50
BR = 250.0
BANDS = {"[0.93,0.95]": (0.93, 0.95), "[0.90,0.95]": (0.90, 0.95)}


def capped_backtest(df: pd.DataFrame, band: tuple[float, float]) -> dict:
    """Deployed rule: per date, in-band favorites -> top-NC by price, 50% split, compound, fee-aware.
    Fee = the per-contract Kalshi fee 0.07*P*(1-P), applied as a price markup
    price*(1+0.07*(1-P)); same convention as band_quality + directional_paper + exec_model.
    NOTE: does NOT model the engine's account drawdown halt — a single -50% day would latch it and
    freeze trading, so a widened band's post-crash PnL here is COUNTERFACTUAL (overstated)."""
    bal = BR
    eq = [bal]
    daily = []
    pk = wn = am = 0
    days = 0
    for _, day in df.sort_values("date").groupby("date"):
        cand = day[(day.price >= band[0]) & (day.price <= band[1])].sort_values("price", ascending=False)
        if cand.empty:
            continue
        days += 1
        chosen = cand.head(NC)
        if not chosen["win"].any():
            am += 1
        per = STAKE / len(chosen)
        pay = 0.0
        for c in chosen.itertuples():
            pay += per * bal / (c.price * (1 + 0.07 * (1 - c.price))) if c.win else 0.0
            pk += 1
            wn += int(c.win)
        bal = bal - STAKE * bal + pay
        daily.append(bal - eq[-1])
        eq.append(bal)
        if bal < 1e-9:
            break
    eq = np.array(eq)
    daily = np.array(daily)
    peak = np.maximum.accumulate(eq)
    return dict(days=days, entries=pk, hit=wn / pk if pk else float("nan"), final=bal, pnl=bal - BR,
                dd=float((eq / peak - 1).min()) if len(eq) > 1 else 0.0,
                worst=float(daily.min()) if len(daily) else 0.0, allmiss=am,
                perday=(bal - BR) / days if days else 0.0)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    ph = k / n
    den = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / den
    hw = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - hw), min(1.0, c + hw))


def band_quality(df: pd.DataFrame, lo: float, hi: float, hi_inclusive: bool) -> dict:
    s = df[(df.price >= lo) & (df.price <= hi)] if hi_inclusive else df[(df.price >= lo) & (df.price < hi)]
    n = len(s)
    if n == 0:
        return dict(n=0)
    k = int(s.win.sum())
    hit = k / n
    mp = float(s.price.mean())
    fee = float((0.07 * s.price * (1 - s.price)).mean())  # continuous per-contract fee, dollars
    lo_ci, hi_ci = wilson(k, n)
    # net EV per $1 contract = win_rate - price - fee ; "edge over fair" = win_rate - price
    return dict(n=n, hit=hit, k=k, mean_price=mp, fee=fee, edge=hit - mp, net_ev=hit - mp - fee,
                wilson=(lo_ci, hi_ci))


def main() -> int:
    d = pd.read_parquet(CACHE)
    d = d[d["eval"] == "anchor"].copy()
    d["date"] = pd.to_datetime(d["date"])
    for market in ("high", "low"):
        m = d[d.market == market]
        print(f"\n{'='*82}\n{market.upper()}-TEMP  |  anchor (deployed model), 2026, last-trade  |  N_events={len(m)}\n{'='*82}")

        print("CAPPED STRATEGY ($250, top-3, 50% split, compound, fee-aware):")
        print(f"  {'band':<14}{'days':>5}{'entries':>8}{'hit':>7}{'$final':>9}{'PnL':>8}{'maxDD':>7}{'worst':>7}{'allmiss':>8}{'$/day':>7}")
        for name, b in BANDS.items():
            r = capped_backtest(m, b)
            print(f"  {name:<14}{r['days']:>5}{r['entries']:>8}{r['hit']:>7.1%}{r['final']:>9.0f}"
                  f"{r['pnl']:>+8.0f}{r['dd']:>7.0%}{r['worst']:>+7.0f}{r['allmiss']:>5}/{r['days']:<3}{r['perday']:>+7.2f}")

        print("\nBAND QUALITY (every in-band favorite, hit% vs price; EV per $1-contract):")
        print(f"  {'sub-band':<14}{'n':>5}{'hit%':>7}{'mean_px':>9}{'edge(hit-px)':>13}{'net_EV':>9}{'  Wilson95(hit)'}")
        rows = [("[0.90,0.93)", 0.90, 0.93, False), ("[0.93,0.95]", 0.93, 0.95, True),
                ("[0.90,0.95]", 0.90, 0.95, True)]
        for name, lo, hi, inc in rows:
            q = band_quality(m, lo, hi, inc)
            if q["n"] == 0:
                print(f"  {name:<14}{'0':>5}  (no events)"); continue
            w = q["wilson"]
            flag = "  +EV" if q["net_ev"] > 0 else "  -EV"
            print(f"  {name:<14}{q['n']:>5}{q['hit']:>7.1%}{q['mean_price']:>9.3f}{q['edge']:>+13.3f}"
                  f"{q['net_ev']:>+9.3f}   [{w[0]:.2%},{w[1]:.2%}]{flag}")

        # temporal stability of the MARGINAL sub-band hit% (first half vs second half of dates)
        sub = m[(m.price >= 0.90) & (m.price < 0.93)].sort_values("date")
        if len(sub) >= 8:
            half = len(sub) // 2
            h1, h2 = sub.iloc[:half], sub.iloc[half:]
            print(f"\n  [0.90,0.93) STABILITY: 1st half n={len(h1)} hit={h1.win.mean():.1%} (<= {h1.date.max().date()})"
                  f"  |  2nd half n={len(h2)} hit={h2.win.mean():.1%} (>= {h2.date.min().date()})")
    print("\nDecision rule: widen ONLY if [0.90,0.93) net_EV > 0 with a Wilson lower bound above the"
          "\nfee, AND it survives the real-ask haircut (last-trade overstates; sub-0.93 favorites are"
          "\nthinner -> wider spread). Coverage (more entries) is NOT edge (lesson #13).")
    print("CAVEATS (per stats review 2026-06-15): the Wilson CI above is PER-ENTRY, not day-clustered"
          "\n(entries cluster by day), so it OVERSTATES significance. Day-clustered, HIGH [0.90,0.93)"
          "\nis z_DAY~0.83 (p~0.20) and its WR lower bound sits below break-even -> NOT a robust edge"
          "\neven before the ask haircut. The capped maxDD is dominated by single thin-0.90-favorite"
          "\nsingle-entry days (50% on one undiversified leg) that the narrow band excludes; those"
          "\n-50%/-51% days also latch the account halt (which this sim does not model).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
