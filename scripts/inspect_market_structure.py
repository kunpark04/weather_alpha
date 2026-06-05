"""Inspect how KXHIGHCHI market STRUCTURE evolved 2021->2026: threshold-ladder vs 6-bucket range.

The wing strategy assumes each event = 4 range buckets + 2 tails (6 mutually-exclusive markets,
subtitles like '72-73' / '<=71' / '>=80'). Early markets may instead be a ladder of independent
threshold contracts ('T81' = high >= 81?). If so, those days aren't wing-backtestable and must be
identified. Groups all historical markets for the series by event and reports, by quarter:
  mkts/event (mean/median/mode), strike_type mix, and a few sample subtitles per era.
Public API, no auth. Read-only.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.backfill_historical import all_historical_markets, live_settled_markets
from scripts.kalshi_history import parse_event_date


def main():
    series = sys.argv[1] if len(sys.argv) > 1 else "KXHIGHCHI"
    mk = all_historical_markets(series) + live_settled_markets(series)
    by_ticker = {m.get("ticker"): m for m in mk if m.get("ticker")}
    print(f"{series}: {len(by_ticker)} markets")

    rows = []
    for m in by_ticker.values():
        et = m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0]
        ed = parse_event_date({"event_ticker": et})
        if ed is None:
            continue
        rows.append({"event": et, "date": ed, "ticker": m["ticker"],
                     "strike_type": m.get("strike_type"), "subtitle": m.get("subtitle") or "",
                     "floor": m.get("floor_strike"), "cap": m.get("cap_strike")})
    df = pd.DataFrame(rows)
    df["q"] = df["date"].dt.to_period("Q").astype(str)

    per_event = df.groupby("event").agg(date=("date", "first"), n=("ticker", "size"),
                                        stypes=("strike_type", lambda s: tuple(sorted(set(s.dropna())))))
    per_event["q"] = per_event["date"].dt.to_period("Q").astype(str)

    print("\nMarkets-per-event by quarter (6 ~ range-bucket era; many/few ~ threshold ladder):")
    print(f"  {'quarter':<9}{'events':>7}{'mean':>7}{'med':>6}{'mode':>6}{'min':>5}{'max':>5}")
    for q, g in per_event.groupby("q"):
        ns = g["n"].values
        mode = Counter(ns).most_common(1)[0][0]
        print(f"  {q:<9}{len(g):>7}{ns.mean():>7.1f}{int(np.median(ns)):>6}{mode:>6}{ns.min():>5}{ns.max():>5}")

    print("\nstrike_type mix by quarter:")
    for q, g in df.groupby("q"):
        c = Counter(g["strike_type"].dropna())
        print(f"  {q:<9} {dict(c)}")

    # sample one event per year and dump its full bucket set
    print("\nSample event structure across years (ticker suffix | strike_type | subtitle):")
    for yr in [2021, 2022, 2023, 2024, 2025, 2026]:
        sub = per_event[per_event["date"].dt.year == yr]
        if sub.empty:
            continue
        # pick a mid-year event with the modal market count for that year
        ev = sub.sort_values("date").iloc[len(sub) // 2].name
        g = df[df["event"] == ev].sort_values("ticker")
        print(f"\n  {ev} ({len(g)} markets):")
        for _, r in g.iterrows():
            suf = r["ticker"].rsplit("-", 1)[-1]
            print(f"    {suf:<8} {str(r['strike_type']):<12} {r['subtitle'][:48]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
