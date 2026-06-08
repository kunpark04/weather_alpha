"""Per-city intraday edge by offset -- the de-pooled view (you asked: trade only +EV cities AS
TIME PASSES, not the pooled aggregate). Reuses collect()/_agg from intraday_decay_backtest.

TWO readings, do NOT conflate:
 * DIAGNOSTIC (this script): per-city REALIZED edge at each offset, to SEE which cities are +EV
   when. 'firstPos' (first offset a city's realized edge>0) uses coverage known only at settlement
   = LOOK-AHEAD; it is NOT a live signal, and late crossings are contaminated by the daily high
   being partly realized. Low-n cells (<8 fired days) flagged '*' -- noisy, not hidden.
 * IMPLEMENTABLE (separate walk-forward test, not here): gate each city on cost+fee <
   assumed_cov_city with assumed_cov_city a TRAILING/train estimate -> '+EV' becomes the firing
   rule, losers self-deselect, no look-ahead. That OOS test is what should drive the build.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.intraday_decay_backtest import collect, _agg, LABEL

SHOW = [0, 30, 60, 120]            # 1:00, 1:30, 2:00, 3:00 local
MIN_N = 8                          # below this a per-city per-offset edge is too noisy to call


def _cell(a: dict) -> str:
    if a["n"] == 0 or np.isnan(a["edge"]):
        return f"{'--':>14}"
    flag = "*" if a["n"] < MIN_N else " "
    return f"{a['edge']:>+8.3f}{flag}(n{a['n']:>2})"


def main():
    rec = collect()
    if rec.empty:
        print("no data"); return 1

    per = {}
    for s, g in rec.groupby("series"):
        per[s] = {"city": g["city"].iloc[0], "cells": {o: _agg(g[g.offset == o]) for o in SHOW}}
    for s, d in per.items():
        fp = None
        for o in SHOW:
            a = d["cells"][o]
            if a["n"] >= MIN_N and not np.isnan(a["edge"]) and a["edge"] > 0:
                fp = o; break
        d["firstPos"] = fp

    def key0(s):
        a = per[s]["cells"][0]
        return a["edge"] if (a["n"] >= MIN_N and not np.isnan(a["edge"])) else -9.0
    order = sorted(per, key=key0, reverse=True)

    print("=" * 92)
    print("PER-CITY edge by entry offset (realized, per-$1).  '*' = n<8 (noisy).  firstPos = first")
    print("offset realized edge>0 (LOOK-AHEAD diagnostic, not a live signal).")
    print("=" * 92)
    hdr = "".join(f"{LABEL[o]:>14}" for o in SHOW)
    print(f"{'city':<15}{hdr}{'firstPos':>10}")
    for s in order:
        d = per[s]
        line = "".join(_cell(d["cells"][o]) for o in SHOW)
        fp = "none" if d["firstPos"] is None else LABEL[d["firstPos"]]
        print(f"{d['city']:<15}{line}{fp:>10}")

    print("\n# of cities with realized edge>0 at each offset (denominator = cities with n>=8 there):")
    for o in SHOW:
        elig = [per[s]["cells"][o] for s in per if per[s]["cells"][o]["n"] >= MIN_N]
        pos = sum(1 for a in elig if not np.isnan(a["edge"]) and a["edge"] > 0)
        print(f"  {LABEL[o]:>5}: {pos:>2}/{len(elig):>2} cities +EV   "
              f"(median edge {np.nanmedian([a['edge'] for a in elig]):+.3f})")

    print("\nThe count rises over time partly because losing cities' cheap-but-stale wings drop out")
    print("and partly because the high is being realized -- NOT proof a live per-city gate captures")
    print("it. firstPos for a late-crossing city is exactly the contaminated/look-ahead case.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
