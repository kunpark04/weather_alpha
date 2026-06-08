"""Per-city CUSHION = top-2 coverage - ask-sum - fee, by anchor hour -- the non-misleading selector
(top-2 alone has no cost, so it ranks the wrong markets; LA tops top-2 yet loses).

For the 2 HIGHEST-PRICED buckets at each hour (the coherent 'buy top-2' wing, ~ the production wing):
  coverage = P(settled winner in those 2)         [= the top-2 you already saw]
  ask-sum S = sum of their 2 asks (last-trade + 1c proxy)   [what you PAY per $1 payout]
  fee       = sum 0.07*a*(1-a) per $1
  cushion   = coverage - S - fee  (= edge per $1; >0 => the wing beats its price)

Reuses modal_rank_multicity load/rank. Prints the cushion-by-hour grid (all cities) and the
top-2 / ask-sum / cushion detail at the 1 PM anchor, sorted by CUSHION (the correct ranking).

CAVEATS: last+1c ask proxy => optimistic (real spreads push S up, cushion down); one season; the
late-hour cushion rise is partly the high being realized (observation, not tradeable). Low-N late.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.modal_rank_multicity import BACKFILL, CITY, load_city

HOURS = [8, 10, 11, 12, 13, 14, 15, 16]


def hlab(h):
    return f"{h if h < 12 else (h - 12 or 12)}{'A' if h < 12 else 'P'}"


def city_rows(series: str) -> pd.DataFrame:
    df = load_city(series)
    if df.empty:
        return pd.DataFrame()
    tz = ZoneInfo(CITY[series][1])
    rows = []
    for ev, day in df.groupby("event_date"):
        winners = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(winners) != 1:
            continue
        win = winners[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        for h in HOURS:
            anchor = pd.Timestamp(ev.year, ev.month, ev.day, h, tz=tz).tz_convert("UTC")
            pre = day[day["timestamp"] <= anchor]
            if pre.empty:
                continue
            last = pre.groupby("ticker")["yes_price_cents"].last()
            if last.size < 2:
                continue
            ranked = last.sort_values(ascending=False)
            top2 = list(ranked.index[:2])
            asks = [min(ranked[t] / 100.0 + 0.01, 0.99) for t in top2]   # last + 1c ask proxy
            rows.append({"hour": h, "cov": int(win in top2),
                         "S": float(sum(asks)),
                         "fee": float(sum(0.07 * a * (1 - a) for a in asks))})
    return pd.DataFrame(rows)


def main():
    agg = {}
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        r = city_rows(s)
        if r.empty:
            continue
        g = r.groupby("hour").agg(n=("cov", "size"), cov=("cov", "mean"),
                                  S=("S", "mean"), fee=("fee", "mean"))
        g["cushion"] = g["cov"] - g["S"] - g["fee"]
        agg[s] = g

    def cush13(s):
        g = agg[s]
        return float(g.loc[13, "cushion"]) if 13 in g.index else -9.0
    order = sorted(agg, key=cush13, reverse=True)

    print("CUSHION = top-2 coverage - ask-sum - fee, by anchor hour (per $1 payout; >0 = beats price)")
    print("=" * 86)
    print(f"{'city':<14}" + "".join(hlab(h).rjust(8) for h in HOURS))
    for s in order:
        g = agg[s]
        cells = "".join((f"{g.loc[h, 'cushion']:>+8.3f}" if h in g.index else f"{'-':>8}") for h in HOURS)
        print(f"{CITY[s][0]:<14}{cells}")

    print("\nAt the 1 PM anchor, sorted by CUSHION (the correct ranking -- contrast with top-2 order):")
    print("=" * 86)
    print(f"{'city':<14}{'N':>5}{'top2':>8}{'ask-sum':>9}{'fee':>7}{'cushion':>10}")
    for s in order:
        g = agg[s]
        if 13 not in g.index:
            continue
        r = g.loc[13]
        print(f"{CITY[s][0]:<14}{int(r['n']):>5}{r['cov']:>8.0%}{r['S']:>9.3f}{r['fee']:>7.3f}{r['cushion']:>+10.3f}")

    print("\ncushion = the edge per $1. NOTE the reorder vs the top-2 table: high coverage with a high")
    print("ask-sum (LA, NYC) gives ~0 / negative cushion; the winners are where coverage CLEARS price.")
    print("Proxy is optimistic (real spreads cut cushion); late-hour rise is realized-high, not edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
