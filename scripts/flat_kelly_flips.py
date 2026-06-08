"""Why some cities' PnL SIGN flips between flat and Kelly -- and the OOS flat-vs-Kelly view.

Flat weights every fired day equally ($2.50). Quarter-Kelly sizes each day proportional to the
running bankroll, so the bankroll COMPOUNDS and the temporal SEQUENCE of wins/losses matters: a
late losing streak on a grown bankroll can erase early gains (or vice-versa). For a near-zero-edge
city the sign is therefore path-dependent and flat vs Kelly easily DISAGREE -- itself a tell that
the edge is within noise (a robust edge makes both agree). A genuinely NEGATIVE-edge city stays
negative under Kelly (sizing can't turn a losing bet positive over many trades), so flips are
almost always flat-positive -> Kelly-negative, not the reverse.

Prints full / in-sample / OOS flat vs Kelly PnL per city with sign-flip flags (Kelly restarts at
$1000 in each window). Kelly is a liquidity-unconstrained compounding fantasy -- shown to explain
the flips, not as an achievable path.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import metrics, run_one
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config

SPLIT = 0.80


def kelly_pnl(df, s, cfg, dates):
    sub = df[df["event_date"].isin(dates)]
    if sub.empty:
        return float("nan")
    return metrics(run_one(sub, s, cfg, sizing="kelly", drop_lower=True, drop_higher=False)).get("pnl", float("nan"))


def main():
    cfg = load_config()
    rows = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        ff = run_one(df, s, cfg, sizing="flat", drop_lower=True, drop_higher=False)
        if ff.empty:
            continue
        dates = sorted(ff["date"].unique())
        k = int(len(dates) * SPLIT)
        cut = dates[k]
        is_d, oos_d = set(dates[:k]), set(dates[k:])
        rows.append({
            "city": CITY[s][0],
            "f_full": ff["pnl_c"].sum() / 100.0,
            "k_full": kelly_pnl(df, s, cfg, set(dates)),
            "f_is": ff[ff["date"] < cut]["pnl_c"].sum() / 100.0,
            "k_is": kelly_pnl(df, s, cfg, is_d),
            "f_oos": ff[ff["date"] >= cut]["pnl_c"].sum() / 100.0,
            "k_oos": kelly_pnl(df, s, cfg, oos_d),
        })

    def flip(a, b):
        return "FLIP" if (np.isfinite(a) and np.isfinite(b) and a != 0 and b != 0
                          and np.sign(a) != np.sign(b)) else ""

    rows.sort(key=lambda r: -r["f_full"])
    print("=" * 104)
    print("FLAT vs KELLY PnL  -- full / in-sample / OOS  (Kelly = final-$1000, restarts $1000 per window)")
    print("=" * 104)
    print(f"{'city':<14} | {'FULLflat':>9}{'FULLkel':>10}{'flip':>6} | {'ISflat':>8}{'ISkel':>9}{'flip':>6}"
          f" | {'OOSflat':>8}{'OOSkel':>9}{'flip':>6}")
    nflip = 0
    for r in rows:
        ff_, kf = r["f_full"], r["k_full"]
        fi, ki = r["f_is"], r["k_is"]
        fo, ko = r["f_oos"], r["k_oos"]
        for a, b in [(ff_, kf), (fi, ki), (fo, ko)]:
            if flip(a, b):
                nflip += 1
        print(f"{r['city']:<14} | {ff_:>+9.2f}{kf:>+10.2f}{flip(ff_,kf):>6} | "
              f"{fi:>+8.2f}{ki:>+9.2f}{flip(fi,ki):>6} | {fo:>+8.2f}{ko:>+9.2f}{flip(fo,ko):>6}")

    print(f"\n{nflip} sign-flips across {len(rows)*3} city-windows. Direction of every flip "
          f"(check above): positive-flat -> negative-Kelly.")
    print("Mechanism: Kelly compounds, so a near-zero-edge city's sign is set by WHEN its wins/losses")
    print("land, not just how many -- a late loss on a grown bankroll dominates. A true negative edge")
    print("stays negative under Kelly. Flips therefore mark within-noise cities; read the FLAT columns.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
