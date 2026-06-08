"""GATED counterpart to cushion_by_hour_allcities: the PRODUCTION wing (modal + drop_lower_ask)
cushion by anchor hour, counting ONLY days the gate fires (wing ask-sum < 0.90). This is the
version that reconciles with backtest_by_city (the all-cities 1 PM gated backtest). Reuses
wing_edge_by_hour.wing_cs (the same ungated wing build; the gate is applied here).

Per city per hour:
  fire%    = fraction of days the wing FIRES (its 2-leg ask-sum < 0.90, strict, matching production)
  coverage = winner inside the 2-leg wing, on FIRED days
  ask-sum  = mean 2-leg cost on fired days
  cushion  = coverage - ask-sum - fee  (= edge per $1 = what the gated backtest realizes)

vs the UNGATED top-2 table: the gate skips expensive days, cutting mean cost from ~0.92 (all days)
to ~0.83 (fired days) -- that ~9pt cut IS the edge. Uses the production wing (a cheaper pair than
the raw 2 highest-priced), so it fires where top-2 would not. Caveats: last+1c proxy (optimistic),
one season, late-hour numbers thin + realized-high.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.wing_edge_by_hour import wing_cs
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config

HOURS = [8, 10, 11, 12, 13, 14, 15, 16]
GATE = 0.90


def hlab(h):
    return f"{h if h < 12 else (h - 12 or 12)}{'A' if h < 12 else 'P'}"


def hour_stats(d: pd.DataFrame, h: int):
    g = d[d.hour == h]
    if g.empty:
        return None
    f = g[g.fired]
    fire = len(f) / len(g)
    if f.empty:
        return {"fire": fire, "nf": 0, "cov": np.nan, "S": np.nan, "cush": np.nan}
    cov, S, fee = f["cov"].mean(), f["S"].mean(), f["fee"].mean()
    return {"fire": fire, "nf": len(f), "cov": cov, "S": S, "cush": cov - S - fee}


def main():
    cfg = load_config()
    per = {}
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        tz = ZoneInfo(CITY[s][1])
        recs = []
        for ev, day in df.groupby("event_date"):
            w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
            if len(w) != 1:
                continue
            winner = w[0]
            for h in HOURS:
                anchor = pd.Timestamp(ev.year, ev.month, ev.day, h, tz=tz).tz_convert("UTC")
                r = wing_cs(day, winner, anchor, cfg, ev)
                if r is None:
                    continue
                recs.append({"hour": h, "fired": r["S"] < GATE, **r})
        if recs:
            per[s] = pd.DataFrame(recs)

    stats = {s: {h: hour_stats(per[s], h) for h in HOURS} for s in per}

    def cush13(s):
        st = stats[s].get(13)
        return st["cush"] if (st and st["nf"] and not np.isnan(st["cush"])) else -9.0
    order = sorted(per, key=cush13, reverse=True)

    print("GATED CUSHION = coverage - ask-sum - fee on FIRED days (wing ask-sum < 0.90), by hour (per $1)")
    print("=" * 86)
    print(f"{'city':<14}" + "".join(hlab(h).rjust(8) for h in HOURS))
    for s in order:
        cells = ""
        for h in HOURS:
            st = stats[s][h]
            cells += (f"{st['cush']:>+8.3f}" if (st and st["nf"] and not np.isnan(st["cush"])) else f"{'-':>8}")
        print(f"{CITY[s][0]:<14}{cells}")

    print("\nnFired = sample size (days) behind each cushion cell above:")
    print("=" * 86)
    print(f"{'city':<14}" + "".join(hlab(h).rjust(8) for h in HOURS))
    for s in order:
        cells = "".join((f"{stats[s][h]['nf']:>8}" if stats[s][h] else f"{'-':>8}") for h in HOURS)
        print(f"{CITY[s][0]:<14}{cells}")

    print("\nAt the 1 PM anchor, sorted by gated cushion (reconciles with backtest_by_city):")
    print("=" * 86)
    print(f"{'city':<14}{'fire%':>7}{'nFired':>8}{'cover':>8}{'ask-sum':>9}{'cushion':>10}")
    for s in order:
        st = stats[s].get(13)
        if not st:
            continue
        if st["nf"] == 0:
            print(f"{CITY[s][0]:<14}{st['fire']:>7.0%}{0:>8}{'--':>8}{'--':>9}{'--':>10}")
        else:
            print(f"{CITY[s][0]:<14}{st['fire']:>7.0%}{st['nf']:>8}{st['cov']:>8.0%}"
                  f"{st['S']:>9.3f}{st['cush']:>+10.3f}")

    print("\nGated => positive cushion for the ~7 cities backtest_by_city flagged profitable; the gate")
    print("cuts cost (~0.92 all-day -> ~0.83 fired) = the edge. fire% shows LA/NYC barely trade (their")
    print("wings rarely fall under 0.90). Cushion still knees ~midday; late cols thin + realized-high.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
