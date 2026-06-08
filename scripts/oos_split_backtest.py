"""80:20 in-sample/OOS TEMPORAL split of the production wing backtest, per city, on the deep
(2023+) history -- the validation the 67-day window could never support.

IS  = each city's first 80% of event-days (the period you'd use to DECIDE which cities to trade)
OOS = the held-out last 20% (never used to choose anything)

Tests whether an in-sample edge GENERALIZES out of sample:
  1. per-city IS vs OOS edge (cent/position) + PnL (flat $2.50, drop_lower_ask = live config)
  2. IS-edge -> OOS-edge rank correlation across cities (does IS predict OOS?)
  3. BASKET: pick the cities with IS edge>0, then measure THAT basket's OOS PnL (signal) vs its
     OOS placebo, vs the rejected (IS edge<=0) basket's OOS PnL -> does selecting on IS pay OOS?
  4. signal (drop_lower_ask) vs placebo (drop_higher_ask) in OOS overall (project rule #3, OOS)

Reuses run_one from backtest_multicity (identical production strategy + gate 0.90 + win_prob 0.92,
all FIXED historically, not refit here). Flat sizing => days are independent, so a post-hoc
temporal partition of the per-day PnL is valid. CAVEAT: shallow cities have tiny OOS slices
(20% of ~120 days ~ 24 days, ~10 fired) -- the deep cities (CHI/NY/MIA/AUS) carry the OOS power;
a MIN_OOS_FIRED gate flags which rows are a real test vs anecdote.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import run_one
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config

SPLIT = 0.80
MIN_OOS_FIRED = 15          # OOS fired-day count below which a city's OOS result is anecdote


def summ(rows: pd.DataFrame | None) -> dict | None:
    if rows is None or rows.empty:
        return None
    pos = int(rows["n_pos"].sum()); wins = int(rows["n_win"].sum())
    pnl = rows["pnl_c"].sum() / 100.0
    dp = rows["pnl_c"].astype(float)
    sharpe = float(dp.mean() / dp.std() * np.sqrt(252)) if dp.std() > 0 else 0.0
    return {"days": len(rows), "fired": int(rows["traded"].sum()), "pos": pos,
            "wr": (wins / pos) if pos else float("nan"), "pnl": pnl,
            "edge_c": (pnl * 100 / pos) if pos else float("nan"), "sharpe": sharpe}


def spearman(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 3:
        return float("nan")
    return float(np.corrcoef(pd.Series(a[m]).rank(), pd.Series(b[m]).rank())[0, 1])


def main():
    cfg = load_config()
    recs = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        sig = run_one(df, s, cfg, sizing="flat", drop_lower=True, drop_higher=False)
        plac = run_one(df, s, cfg, sizing="flat", drop_lower=False, drop_higher=True)
        if sig.empty:
            continue
        dates = sorted(sig["date"].unique())
        cut = dates[int(len(dates) * SPLIT)]
        rec = {"city": CITY[s][0], "series": s, "cut": pd.Timestamp(cut).date(),
               "IS": summ(sig[sig["date"] < cut]), "OOS": summ(sig[sig["date"] >= cut]),
               "OOSp": summ(plac[plac["date"] >= cut])}
        if rec["IS"] and rec["OOS"]:
            recs.append(rec)

    recs.sort(key=lambda r: -(r["IS"]["edge_c"] if np.isfinite(r["IS"]["edge_c"]) else -9e9))

    print("=" * 108)
    print(f"80:20 IN-SAMPLE / OUT-OF-SAMPLE TEMPORAL SPLIT  (per city; flat $2.50; drop_lower_ask; gate 0.90)")
    print("  IS = first 80% of each city's event-days; OOS = held-out last 20%. edge = cents per position.")
    print("=" * 108)
    hdr = (f"{'city':<14}{'cut':>11} | {'IS_fire':>7}{'IS_edge':>8}{'IS_PnL':>9} | "
           f"{'OOSfire':>8}{'OOSedge':>8}{'OOS_PnL':>9}  {'sign':>5}")
    print(hdr); print("-" * 108)
    for r in recs:
        i, o = r["IS"], r["OOS"]
        match = "ok" if (np.isfinite(i["edge_c"]) and np.isfinite(o["edge_c"])
                         and np.sign(i["edge_c"]) == np.sign(o["edge_c"])) else "--"
        thin = "" if o["fired"] >= MIN_OOS_FIRED else "  (thin)"
        print(f"{r['city']:<14}{str(r['cut']):>11} | {i['fired']:>7}{i['edge_c']:>+8.1f}{i['pnl']:>+9.2f} | "
              f"{o['fired']:>8}{o['edge_c']:>+8.1f}{o['pnl']:>+9.2f}  {match:>5}{thin}")

    # ---- IS -> OOS edge correlation (all, and deep-enough OOS) ----
    allc = [(r["IS"]["edge_c"], r["OOS"]["edge_c"]) for r in recs]
    deep = [(r["IS"]["edge_c"], r["OOS"]["edge_c"]) for r in recs if r["OOS"]["fired"] >= MIN_OOS_FIRED]
    print("\nIS-edge -> OOS-edge rank correlation (does in-sample edge predict OOS edge?):")
    print(f"  all cities (n={len(allc)}):                 Spearman {spearman(*zip(*allc)):+.2f}")
    if deep:
        print(f"  OOS fired >= {MIN_OOS_FIRED} (n={len(deep)}):              Spearman {spearman(*zip(*deep)):+.2f}")

    # ---- basket selected on IS only ----
    sel = [r for r in recs if r["IS"]["edge_c"] > 0]
    rej = [r for r in recs if r["IS"]["edge_c"] <= 0]
    def tot(g, key, sub="pnl"):
        return sum(r[key][sub] for r in g if r[key])
    print("\nBASKET picked on IN-SAMPLE edge>0, scored OUT-OF-SAMPLE:")
    print(f"  selected  {len(sel):>2} cities  ->  OOS signal PnL {tot(sel,'OOS'):>+9.2f}   "
          f"OOS placebo PnL {tot(sel,'OOSp'):>+9.2f}")
    print(f"  rejected  {len(rej):>2} cities  ->  OOS signal PnL {tot(rej,'OOS'):>+9.2f}")
    print(f"  ALL       {len(recs):>2} cities  ->  OOS signal PnL {tot(recs,'OOS'):>+9.2f}   "
          f"OOS placebo PnL {tot(recs,'OOSp'):>+9.2f}")

    # deep-OOS-only basket (the statistically meaningful subset)
    seld = [r for r in sel if r["OOS"]["fired"] >= MIN_OOS_FIRED]
    print(f"\n  selected & OOS-deep ({len(seld)} cities: {', '.join(r['city'] for r in seld)}):")
    print(f"     OOS signal PnL {tot(seld,'OOS'):>+9.2f}   vs its OOS placebo {tot(seld,'OOSp'):>+9.2f}")

    print("\nReads: sign 'ok' = OOS edge keeps the IS sign. A positive IS->OOS Spearman + selected-basket")
    print("OOS PnL > rejected-basket means the in-sample edge GENERALIZES; flat ~0 / negative means the")
    print("per-city edge was in-sample overfit (city selection not robust). Deep cities are the real test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
