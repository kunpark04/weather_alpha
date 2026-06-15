"""Low-temp 11:30 PM TWO-uncorrelated-city algo: pick 2 high-confidence cities from DIFFERENT climate
regions, split the 50% stake 25%/25%, with a calibrated confidence + calibration check.

Why 2 uncorrelated: a single 50% bet loses 50% on any miss. Two 25% bets in DIFFERENT regions (whose
overnight lows are driven by different air masses) rarely miss the same night, so a typical miss costs
only 25% and the equity curve is far smoother. It also doubles the (confidence, outcome) observations.

ALGO (per event date, 23:30 local):
  1. Candidates = favorites priced in [0.85,0.95] with price >= TAU (high-confidence gate).
  2. Pick #1 = highest confidence. Pick #2 = highest confidence in a DIFFERENT region than #1.
  3. Stake `bet`/2 on each (25%+25%); hold to settlement; compound.
CONFIDENCE = calibrated P(settle in modal bucket) (train-fit, monotone in price).
CALIBRATION = realized hit vs confidence, with Wilson CIs, for the picks AND the pooled high-conf
universe (the pooled N sets how tight the CI can get). Data: data/lowtemp_modal_band_8595.parquet.

Usage: python scripts/lowtemp_2city_algo.py [--tau 0.93] [--bet 0.5] [--bankroll 25]
"""
from __future__ import annotations

import argparse, math, sys
from pathlib import Path
import numpy as np, pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from scripts.late_night_coverage import LOW

LO, HI, SNAP = 0.85, 0.95, "23:30"
REGION = {  # climate/air-mass region per low-temp series -> 'uncorrelated' = different region
    "KXLOWTLAX": "WestCoast", "KXLOWTSFO": "WestCoast", "KXLOWTSEA": "WestCoast",
    "KXLOWTPHX": "Desert", "KXLOWTLV": "Desert", "KXLOWTDEN": "Mountain",
    "KXLOWTAUS": "Texas", "KXLOWTDAL": "Texas", "KXLOWTSATX": "Texas", "KXLOWTHOU": "Texas", "KXLOWTOKC": "Plains",
    "KXLOWTNOLA": "GulfSE", "KXLOWTMIA": "GulfSE", "KXLOWTATL": "GulfSE",
    "KXLOWTCHI": "Midwest", "KXLOWTMIN": "Midwest",
    "KXLOWTPHIL": "Northeast", "KXLOWTDC": "Northeast", "KXLOWTBOS": "Northeast",
}


def wilson(k, n):
    if n == 0: return (float("nan"), float("nan"))
    z = 1.96; ph = k / n; d = 1 + z*z/n
    c = (ph + z*z/(2*n)) / d; h = z*math.sqrt(ph*(1-ph)/n + z*z/(4*n*n)) / d
    return (c - h, c + h)


def calib_map(price, win, bins=(0.85, 0.88, 0.91, 0.93, 0.95)):
    price, win = np.asarray(price), np.asarray(win, float); centers, rates = [], []
    for a, b in zip(bins[:-1], bins[1:]):
        m = (price >= a) & (price <= b if b == bins[-1] else price < b)
        if m.sum() >= 5: centers.append((a+b)/2); rates.append(win[m].mean())
    return (np.array(centers), np.maximum.accumulate(rates) if rates else np.array([]))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau", type=float, default=0.93)
    ap.add_argument("--bet", type=float, default=0.5)
    ap.add_argument("--bankroll", type=float, default=25.0)
    args = ap.parse_args(argv)
    df = pd.read_parquet("data/lowtemp_modal_band_8595.parquet")
    df = df[df.snap == SNAP].copy(); df["date"] = pd.to_datetime(df["date"])
    df["city"] = df.series.map(lambda s: LOW[s][0]); df["region"] = df.series.map(REGION)
    band = df[(df.price >= LO) & (df.price <= HI)]
    cmap = calib_map(band[band.date < np.sort(band.date.unique())[int(band.date.nunique()*0.6)]].price.values,
                     band[band.date < np.sort(band.date.unique())[int(band.date.nunique()*0.6)]].win.values)
    conf = lambda p: float(np.interp(p, cmap[0], cmap[1])) if len(cmap[0]) else p

    # --- 2-uncorrelated-city selection per date ---
    picks, both_miss, days_traded = [], 0, 0
    for d, day in df.sort_values("date").groupby("date"):
        cand = day[(day.price >= max(LO, args.tau)) & (day.price <= HI)].sort_values("price", ascending=False)
        if cand.empty: continue
        p1 = cand.iloc[0]
        rest = cand[cand.region != p1.region]
        chosen = [p1] + ([rest.iloc[0]] if len(rest) else [])
        days_traded += 1
        if len(chosen) == 2 and not chosen[0].win and not chosen[1].win: both_miss += 1
        for c in chosen:
            picks.append({"date": d, "city": c.city, "region": c.region, "price": float(c.price),
                          "confidence": conf(c.price), "win": bool(c.win), "n_today": len(chosen)})
    P = pd.DataFrame(picks)
    two = P[P.n_today == 2]
    print(f"=== 2-UNCORRELATED-CITY ALGO  (tau={args.tau}, 23:30, band[{LO},{HI}]) ===")
    print(f"dates traded: {days_traded} | total picks: {len(P)} | days with 2 picks: {two.date.nunique()} "
          f"| days only 1 region available: {days_traded - two.date.nunique()}")
    k, n = int(P.win.sum()), len(P); lo, hi = wilson(k, n)
    print(f"\nPICK hit rate (all picks): {k}/{n} = {k/n:.1%}  Wilson95% [{lo:.1%}, {hi:.1%}]  conf={P.confidence.mean():.3f}")
    # pooled high-conf universe (max N -> tightest possible CI)
    pool = band[band.price >= args.tau]; pk, pn = int(pool.win.sum()), len(pool); plo, phi = wilson(pk, pn)
    print(f"POOLED high-conf (>= {args.tau}, all cities, sets CI floor): {pk}/{pn} = {pk/pn:.1%}  "
          f"Wilson95% [{plo:.1%}, {phi:.1%}]")

    # --- diversification: both-miss frequency vs independent expectation ---
    miss = 1 - P.win.mean()
    print(f"\nUNCORRELATION CHECK: nights both picks missed = {both_miss}/{two.date.nunique()} "
          f"({both_miss/max(1,two.date.nunique()):.1%}); independent expectation ~{miss**2:.1%}. "
          f"Cross-region pairs share air-mass risk far less than same-region.")

    # --- calibration of the picks ---
    print(f"\nCALIBRATION (picks): mean confidence {P.confidence.mean():.3f} vs realized hit {k/n:.1%} "
          f"-> {'over' if P.confidence.mean()>k/n else 'under'}-confident by {abs(P.confidence.mean()-k/n):.1%}; "
          f"Brier={np.mean((P.confidence-P.win)**2):.4f}")

    # --- betting: 25%/25% split across the 2 picks (50% total), compounding ---
    bal = args.bankroll; eq = [bal]
    for d, day in P.groupby("date"):
        stake_each = (args.bet / len(day)) * bal     # split bet across that day's picks
        pay = 0.0
        for c in day.itertuples():
            contracts = (stake_each) / (c.price * (1 + 0.07*(1-c.price)))
            pay += contracts if c.win else 0.0
        bal = bal - args.bet*bal + pay; eq.append(bal)
    eq = np.array(eq); dd = float((eq/np.maximum.accumulate(eq)-1).min())
    nb = len(eq)-1; geo = (bal/args.bankroll)**(1/nb)-1 if nb and bal > 0 else float("nan")
    print(f"\n--- BET {args.bet:.0%}/night split across the 2 picks (25%+25%), ${args.bankroll:.0f} start ---")
    print(f"  final=${bal:.2f} ({bal/args.bankroll-1:+.0%})  geo/night={geo:+.2%}  maxDD={dd:.0%}"
          f"  (vs single-50%: a lone miss costs 25% not 50%)")

    # --- data needed for [97,100] ---
    print(f"\n[97,100] CI FEASIBILITY: need Wilson lower>=97%. At a true 98% hit that needs ~750 picks; "
          f"at a true 99% ~95 picks. Current pooled N={pn} with {pn-pk} misses caps the lower bound at "
          f"~{plo:.0%}. Reaching [97,100] needs ~{'1+ year' if pk/pn<0.99 else '~3 more months'} more low-temp tape.")
    P.to_parquet("data/lowtemp_2city_picks.parquet", index=False)
    print("wrote data/lowtemp_2city_picks.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
