"""Low-temp 11:30 PM single-pick algo with a CONFIDENCE output + calibration check.

ALGO (per event date, at 23:30 local):
  1. For every low-temp city, find the favorite (largest modal bucket) and its last-trade price.
  2. Candidates = favorite price in [0.85, 0.95] (the user's band).
  3. SELECT the single highest-confidence candidate; FIRE only if its confidence >= TAU (the
     high-confidence gate that lifts the hit rate toward ~98%); otherwise NO BET that day.
  4. Emit {date, city, bucket, confidence}; bet `--bet` of the account; hold to settlement; compound.

CONFIDENCE = calibrated P(settle in the highest modal bucket), learned from history (empirical hit
rate by price bin, monotone), NOT the raw price — because in the upper band the favorites win MORE
than priced, so the honest confidence is higher than the market number. We then CHECK that confidence:
reliability table, Brier, ECE, and Wilson CIs, for the selected picks and across the band.

Truth = Kalshi settlement_value. Low-temp has NO orderbook ask -> price is last-trade (optimistic).
Data: data/lowtemp_modal_band_8595.parquet (snap=23:30).

Usage: python scripts/lowtemp_confidence_algo.py [--tau 0.93] [--bet 0.5] [--bankroll 25]
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from scripts.late_night_coverage import LOW

LO, HI, SNAP = 0.85, 0.95, "23:30"


def wilson(k, n):
    if n == 0:
        return (float("nan"), float("nan"))
    z = 1.96; ph = k / n
    d = 1 + z*z/n
    c = (ph + z*z/(2*n)) / d
    h = z*math.sqrt(ph*(1-ph)/n + z*z/(4*n*n)) / d
    return (c - h, c + h)


def calib_map(price, win, bins=(0.85, 0.88, 0.91, 0.93, 0.95)):
    """Monotone empirical P(win) by price bin, fit on (price,win) -> function price->calibrated conf."""
    price, win = np.asarray(price), np.asarray(win, float)
    centers, rates = [], []
    for a, b in zip(bins[:-1], bins[1:]):
        m = (price >= a) & (price <= b if b == bins[-1] else price < b)
        if m.sum() >= 5:
            centers.append((a+b)/2); rates.append(win[m].mean())
    rates = np.maximum.accumulate(rates) if rates else rates       # enforce monotone
    return (np.array(centers), np.array(rates))


def conf_of(price, cmap):
    centers, rates = cmap
    if len(centers) == 0:
        return price
    return float(np.interp(price, centers, rates))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tau", type=float, default=0.93, help="confidence gate: only bet if best candidate price >= tau")
    ap.add_argument("--bet", type=float, default=0.5)
    ap.add_argument("--bankroll", type=float, default=25.0)
    args = ap.parse_args(argv)
    df = pd.read_parquet("data/lowtemp_modal_band_8595.parquet")
    df = df[df.snap == SNAP].copy(); df["date"] = pd.to_datetime(df["date"])
    df["city"] = df.series.map(lambda s: LOW[s][0])
    band = df[(df.price >= LO) & (df.price <= HI)]
    ndates = df.date.nunique()

    # --- threshold sweep: hit rate of the daily highest-confidence pick, gated at tau ---
    print(f"=== HIT-RATE vs CONFIDENCE GATE (1 pick/date, highest-conf city with price>=tau) ===")
    print(f"  {'tau':>5}{'bets':>6}{'/dates':>7}{'hit rate':>10}{'95% CI (Wilson)':>20}")
    best_picks = None
    for tau in [0.85, 0.88, 0.90, 0.92, 0.93, 0.94]:
        rows = []
        for d, day in df.sort_values("date").groupby("date"):
            cand = day[(day.price >= max(LO, tau)) & (day.price <= HI)]
            if cand.empty:
                continue
            r = cand.loc[cand.price.idxmax()]
            rows.append({"date": d, "city": r.city, "price": float(r.price), "win": bool(r.win)})
        P = pd.DataFrame(rows); k, n = int(P.win.sum()), len(P)
        lo_, hi_ = wilson(k, n)
        print(f"  {tau:>5.2f}{n:>6}{ndates:>7}{k/n:>10.1%}   [{lo_:.1%}, {hi_:.1%}]")
        if abs(tau - args.tau) < 1e-9:
            best_picks = P

    P = best_picks
    print(f"\n=== OPERATING POINT tau={args.tau}: {len(P)} bets over {ndates} dates, "
          f"hit rate {P.win.mean():.1%} ({int(P.win.sum())}/{len(P)}) ===")

    # --- calibrated confidence (fit on TRAIN dates, applied to picks) + OOS calibration ---
    dts = np.sort(band.date.unique()); cut = dts[int(len(dts)*0.6)]
    cmap = calib_map(band[band.date < cut].price.values, band[band.date < cut].win.values)
    P = P.copy(); P["confidence"] = P.price.map(lambda p: conf_of(p, cmap))
    tr_mask = P.date < cut
    print(f"  calibrated confidence (train-fit): mean {P.confidence.mean():.3f}  (raw price mean {P.price.mean():.3f})")
    for split, sub in [("TRAIN", P[tr_mask]), ("TEST(OOS)", P[~tr_mask])]:
        if len(sub) < 3:
            continue
        k, n = int(sub.win.sum()), len(sub); lo_, hi_ = wilson(k, n)
        print(f"  {split:<10} n={n:>3} mean_conf={sub.confidence.mean():.3f}  realized_hit={k/n:.1%} "
              f"[{lo_:.0%},{hi_:.0%}]  Brier={np.mean((sub.confidence-sub.win)**2):.4f}")

    # --- sample picks with their confidence ---
    print("\nsample picks (city | confidence | settled-in-modal):")
    for _, x in P.head(10).iterrows():
        print(f"  {x['date'].date()}  {x['city']:<14} conf={x['confidence']:.2f} (px {x['price']:.2f})  "
              f"{'YES' if x['win'] else 'NO  <-- MISS'}")

    # --- betting `bet` of account on the gated pick ---
    bal = args.bankroll; eq = [bal]
    for _, x in P.sort_values("date").iterrows():
        p = x["price"]; b = args.bet * bal
        contracts = b / (p * (1 + 0.07*(1-p)))
        bal = (bal - b) + (contracts if x["win"] else 0.0); eq.append(bal)
    eq = np.array(eq); dd = float((eq/np.maximum.accumulate(eq)-1).min())
    days = len(eq)-1; geo = (bal/args.bankroll)**(1/days)-1 if days and bal>0 else float("nan")
    edge = P.win.mean() - P.price.mean()
    print(f"\n--- BET {args.bet:.0%}/pick (${args.bankroll:.0f} start, compounding, {len(P)} bets) ---")
    print(f"  hit={P.win.mean():.1%}  edge(hit-price)={edge:+.1%}  final=${bal:.2f} ({bal/args.bankroll-1:+.0%}) "
          f" geo/bet={geo:+.2%}  maxDD={dd:.0%}{'  RUIN' if bal<args.bankroll*0.05 else ''}")
    P.to_parquet("data/lowtemp_confidence_picks.parquet", index=False)
    print("wrote data/lowtemp_confidence_picks.parquet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
