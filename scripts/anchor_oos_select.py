"""Consider non-1PM anchors PROPERLY: per-city best anchor chosen IN-SAMPLE, validated OOS.

The raw grid showed edge rising into the afternoon -- but that is realized-high LEAKAGE (coverage
-> 100% by 4 PM), a bias the OOS split CANNOT remove (it is present in both halves). So this test
has TWO controls:
  * leakage: candidate anchors are restricted to the genuine forecast window {12:00,12:30,13:00,13:30}
    (the daily max is rarely in by 1:30; 14:00+ excluded).
  * overfitting: pick each city's best anchor on the IN-SAMPLE 80% only, then score it on the
    held-out OOS 20%. Selecting the max of 4 noisy anchors inflates IS edge; OOS tells the truth.

Per city: best IS anchor, IS/OOS edge at that anchor vs at fixed 1 PM, OOS PnL at both. Question:
does ANY city earn a real, tradeable, OOS-surviving edge at an anchor other than 1 PM -- or is 1 PM
already right and the 'improvements' pure leakage/overfit?
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.anchor_grid import wing_day
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config

ANCHORS = [(12, 0), (12, 30), (13, 0), (13, 30)]      # leakage-free forecast window
ONE_PM = (13, 0)
SPLIT = 0.80
MIN_IS_FIRED = 15


def alab(a):
    h, m = a
    return f"{(h - 12) or 12}:{m:02d}"


def per_day(df, cfg, tz, h, m):
    rows = []
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        anchor = pd.Timestamp(ev.year, ev.month, ev.day, h, m, tz=tz).tz_convert("UTC")
        r = wing_day(day, w[0], anchor, cfg, ev)
        if r is None:
            continue
        if r["fired"]:
            rows.append({"date": ev, "cov": r["cov"], "cost": r["cost"], "fee": r["fee"], "pnl": r["pnl"]})
    return pd.DataFrame(rows)


def summ(d):
    if d is None or d.empty:
        return {"fired": 0, "edge": float("nan"), "pnl": 0.0}
    return {"fired": len(d), "edge": float(d["cov"].mean() - d["cost"].mean() - d["fee"].mean()),
            "pnl": float(d["pnl"].sum())}


def main():
    cfg = load_config()
    recs = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        tz = ZoneInfo(CITY[s][1])
        pa = {a: per_day(df, cfg, tz, *a) for a in ANCHORS}
        dates = sorted(pd.concat([pa[a][["date"]] for a in ANCHORS if not pa[a].empty])["date"].unique())
        if len(dates) < 20:
            continue
        cut = dates[int(len(dates) * SPLIT)]
        is_s = {a: summ(pa[a][pa[a]["date"] < cut]) if not pa[a].empty else summ(None) for a in ANCHORS}
        oos_s = {a: summ(pa[a][pa[a]["date"] >= cut]) if not pa[a].empty else summ(None) for a in ANCHORS}
        cand = [a for a in ANCHORS if is_s[a]["fired"] >= MIN_IS_FIRED and np.isfinite(is_s[a]["edge"])]
        if not cand or is_s[ONE_PM]["fired"] < MIN_IS_FIRED:
            continue
        best = max(cand, key=lambda a: is_s[a]["edge"])
        recs.append({"city": CITY[s][0], "best": best,
                     "is_best": is_s[best], "is_1pm": is_s[ONE_PM],
                     "oos_best": oos_s[best], "oos_1pm": oos_s[ONE_PM]})

    recs.sort(key=lambda r: -r["oos_1pm"]["pnl"])
    print("=" * 110)
    print("PER-CITY ANCHOR SELECTION (in-sample pick, OOS validate; candidates 12:00-13:30, leakage-free)")
    print("=" * 110)
    print(f"{'city':<14}{'bestIS':>7}{'ISedge@best':>12}{'ISedge@1PM':>11} | "
          f"{'OOSedge@best':>13}{'OOSedge@1PM':>12}{'OOSpnl@best':>12}{'OOSpnl@1PM':>11}")
    tot_best = tot_1pm = 0.0
    flips = 0
    for r in recs:
        ib, i1 = r["is_best"], r["is_1pm"]
        ob, o1 = r["oos_best"], r["oos_1pm"]
        tot_best += ob["pnl"]; tot_1pm += o1["pnl"]
        if o1["pnl"] <= 0 < ob["pnl"]:
            flips += 1
        tag = "" if r["best"] == ONE_PM else ""
        print(f"{r['city']:<14}{alab(r['best']):>7}{ib['edge']*100:>+11.1f}c{i1['edge']*100:>+10.1f}c | "
              f"{ob['edge']*100:>+12.1f}c{o1['edge']*100:>+11.1f}c{ob['pnl']:>+12.2f}{o1['pnl']:>+11.2f}")

    print("-" * 110)
    print(f"TOTAL OOS PnL  @ each city's best-IS anchor: ${tot_best:+.2f}   |   @ fixed 1 PM: ${tot_1pm:+.2f}")
    print(f"cities that flip 1PM-OOS-loss -> best-anchor-OOS-profit: {flips} / {len(recs)}")
    n_moved = sum(1 for r in recs if r["best"] != ONE_PM)
    print(f"cities whose best IS anchor != 1 PM: {n_moved} / {len(recs)}")
    print("\nREAD: if 'TOTAL OOS @ best' barely beats (or loses to) 'OOS @ 1PM', the in-sample anchor")
    print("edge was overfit -- picking the best of 4 anchors on 80% does not carry to the holdout.")
    print("Few/zero loss->profit flips => other anchors do NOT rescue the losing cities within the")
    print("tradeable window; their afternoon 'improvement' was realized-high leakage, not edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
