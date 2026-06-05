"""OOS version of the flat-vs-Kelly table: split each city's deep history 80:20 by date, then run
the production wing (drop_lower_ask) FRESH on each window (Kelly restarts at $1000 in each), so the
out-of-sample flat + Kelly results are clean (no in-sample compounding leaking in).

Mirrors backtest_multicity's flat/Kelly columns but per window. Prints the OOS table (the answer to
"show the OOS version") and the IS table for contrast. CAVEATS: shallow cities have tiny OOS windows
(~24 days); Kelly remains a liquidity-unconstrained compounding fantasy even OOS -- shown only to echo
the edge ranking and the variance, not as an achievable path. Flat $2.50 is the live config.
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


def win(df, s, cfg, dates, sizing):
    sub = df[df["event_date"].isin(dates)]
    if sub.empty:
        return {}
    return metrics(run_one(sub, s, cfg, sizing=sizing, drop_lower=True, drop_higher=False))


def main():
    cfg = load_config()
    recs = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        full = run_one(df, s, cfg, sizing="flat", drop_lower=True, drop_higher=False)
        if full.empty:
            continue
        dates = sorted(full["date"].unique())
        k = int(len(dates) * SPLIT)
        is_d, oos_d = set(dates[:k]), set(dates[k:])
        recs.append({"city": CITY[s][0],
                     "IS_flat": win(df, s, cfg, is_d, "flat"), "IS_kelly": win(df, s, cfg, is_d, "kelly"),
                     "OOS_flat": win(df, s, cfg, oos_d, "flat"), "OOS_kelly": win(df, s, cfg, oos_d, "kelly")})

    def g(m, k, d=float("nan")):
        return m.get(k, d) if m else d

    def show(tag, fk, kk):
        print("\n" + "=" * 96)
        print(f"{tag}: FLAT $2.50  vs  QUARTER-KELLY (start $1000)   -- production wing, drop_lower_ask")
        print("=" * 96)
        print(f"{'city':<14}{'days':>5}{'fire':>6}{'pos':>5}{'WR':>6} | "
              f"{'flatPnL':>9}{'flShrp':>7}{'flDD$':>8} | {'kelPnL':>11}{'kelFinal':>11}{'kelDD%':>8}")
        rows = sorted(recs, key=lambda r: -(g(r[fk], "pnl", -9e9)))
        for r in rows:
            f, kl = r[fk], r[kk]
            if not f:
                continue
            print(f"{r['city']:<14}{int(g(f,'days',0)):>5}{g(f,'fire',0):>5.0%}{int(g(f,'pos',0)):>5}"
                  f"{g(f,'wr',float('nan')):>6.0%} | {g(f,'pnl',0):>+9.2f}{g(f,'sharpe',0):>7.2f}"
                  f"{g(f,'dd_usd',0):>+8.2f} | {g(kl,'pnl',0):>+11.2f}{g(kl,'final',0):>11.0f}"
                  f"{g(kl,'dd_pct',0):>7.1f}%")
        tot_f = sum(g(r[fk], "pnl", 0) for r in recs if r[fk])
        print(f"{'-'*96}\nTOTAL flat PnL ${tot_f:+.2f}")

    show("OUT-OF-SAMPLE (held-out last 20%)", "OOS_flat", "OOS_kelly")
    show("IN-SAMPLE (first 80%, for contrast)", "IS_flat", "IS_kelly")
    print("\nKelly final$ is a compounding fantasy (ignores Kalshi liquidity + the -60/-95% DDs); read")
    print("the FLAT columns. The honest deployment read is the OOS flat PnL/Sharpe per city.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
