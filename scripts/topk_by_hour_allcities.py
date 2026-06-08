"""All-cities anchor-hour knee in TOP-1/2/3 terms (the clean cousin of the wing's 'cover c').

Reuses modal_rank_multicity.city_hourly. top-k = UNCONDITIONAL P(settled winner is in the k
highest-PRICED buckets at that hour) -- truth = Kalshi settlement_value. Prints a top-2-by-hour
grid (the knee, per city) and the top-1/2/3 detail at the 1 PM anchor.

Note vs the earlier 'cover c': cover c = winner inside the 2-leg POSITIONAL wing the strategy buys
(modal + 1 adjacent); top-2 = winner in the 2 highest-priced buckets regardless of adjacency. Same
idea, cleaner + unconditional; they nearly coincide because temperature prices are single-peaked.
top-k is the COVERAGE side only -- it has no cost term, so a high top-k late in the day is NOT free
(the price has risen to match, and the late rise is partly the high already being realized).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.modal_rank_multicity import BACKFILL, CITY, city_hourly

HOURS = [8, 10, 11, 12, 13, 14, 15, 16]


def hlab(h):
    return f"{h if h < 12 else (h - 12 or 12)}{'A' if h < 12 else 'P'}"


def main():
    data = {}
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        g = city_hourly(s)
        if not g.empty:
            data[s] = g.set_index("hour")
    if not data:
        print("no data"); return 1

    def at13(s, col):
        g = data[s]
        return float(g.loc[13, col]) if 13 in g.index else -1.0
    order = sorted(data, key=lambda s: -at13(s, "top2"))

    print("TOP-2 by anchor hour  (unconditional P[winner in the 2 highest-priced buckets]) -- the knee")
    print("=" * 78)
    print(f"{'city':<14}" + "".join(hlab(h).rjust(6) for h in HOURS) + f"{'peak':>6}")
    for s in order:
        g = data[s]
        cells, best = "", (-1.0, None)
        for h in HOURS:
            if h in g.index:
                v = float(g.loc[h, "top2"]); cells += f"{v:>6.0%}"
                if v > best[0]:
                    best = (v, h)
            else:
                cells += f"{'-':>6}"
        print(f"{CITY[s][0]:<14}{cells}{(hlab(best[1]) if best[1] else '-'):>6}")

    print("\nTOP-1 / TOP-2 / TOP-3 at the 1 PM anchor  (all cities, sorted by top-2)")
    print("=" * 78)
    print(f"{'city':<14}{'N':>5}{'top1':>8}{'top2':>8}{'top3':>8}")
    for s in order:
        g = data[s]
        if 13 not in g.index:
            continue
        r = g.loc[13]
        print(f"{CITY[s][0]:<14}{int(r['n']):>5}{r['top1']:>8.0%}{r['top2']:>8.0%}{r['top3']:>8.0%}")

    print("\ntop-k is COVERAGE only (no cost). High top-k late = partly the high already realized +")
    print("the price has risen to match, so it is NOT tradeable edge. The decision knee is ~midday,")
    print("where top-k has plateaued while entries are still genuine forecasts. Late cols are low-N.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
