"""Is the 'edge' metric honest as the anchor moves later? Chicago only (individual basis).

edge = coverage - cost - fee  (EV per $1 payout); the backtest PnL is PROPORTIONAL to it, so a
rising edge IS a rising backtest PnL -- they do not disagree. The question is whether the COST
input is real. cost uses yes_ask ~= (last trade + 1c), a PROXY. As the day's high gets realized:
  * coverage inflates toward 100%  (you're reading the answer, not forecasting)
  * the eventual winner's price climbs (the answer is pre-priced into the book)
  * the last trade used can be stale on a thinning book -> the +1c ask understates the true ask
so the late edge can be a proxy artifact rather than capturable money. This prints, per anchor:
  IS edge, OOS edge (does it survive the holdout?), coverage, mean winner-price-at-anchor
  (how 'known' the answer already is), and median trade staleness (min since last trade used).
Top-2 wing, gate<0.90, flat. 8:00-16:00 local, 30-min.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.anchor_grid_wide import evaluate
from scripts.backtest_multicity import _snapshot
from scripts.modal_rank_multicity import load_city
from weather_alpha.config import load_config

CT = ZoneInfo("America/Chicago")
ANCHORS = [(h, m) for h in range(8, 17) for m in (0, 30)][:17]
SPLIT = 0.80


def alab(a):
    h, m = a
    return f"{(h - 12) or 12 if h >= 12 else h}:{m:02d}{'p' if h >= 12 else 'a'}"


def main():
    cfg = load_config()
    df = load_city("KXHIGHCHI")
    rows = []
    for a in ANCHORS:
        recs = []
        for ev, day in df.groupby("event_date"):
            w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
            if len(w) != 1:
                continue
            winner = w[0]
            anchor = pd.Timestamp(ev.year, ev.month, ev.day, a[0], a[1], tz=CT).tz_convert("UTC")
            contracts = _snapshot(day, anchor)
            if len(contracts) < 2:
                continue
            r = evaluate(contracts, winner, ev, cfg, drop_lower=True)
            if r is None:
                continue
            # winner price already in the book at the anchor (how 'known' the answer is)
            pre = day[day["timestamp"] <= anchor].dropna(subset=["yes_price_cents"])
            wlast = pre[pre["ticker"] == winner]["yes_price_cents"]
            wpx = float(wlast.iloc[-1]) / 100.0 if len(wlast) else 0.0
            # staleness: minutes from the freshest trade used by the wing to the anchor
            stale = (anchor - pre["timestamp"].max()).total_seconds() / 60.0 if len(pre) else np.nan
            recs.append({"date": ev, "cov": r["cov"], "cost": r["cost"], "fee": r["fee"],
                         "pnl": r["pnl"], "wpx": wpx, "stale": stale})
        if not recs:
            continue
        d = pd.DataFrame(recs)
        dates = sorted(d["date"].unique())
        cut = dates[int(len(dates) * SPLIT)] if len(dates) > 5 else dates[-1]
        is_d, oos_d = d[d["date"] < cut], d[d["date"] >= cut]

        def edge(x):
            return (x["cov"].mean() - x["cost"].mean() - x["fee"].mean()) * 100 if len(x) else np.nan
        rows.append({
            "anchor": alab(a), "fired": len(d),
            "is_edge": edge(is_d), "oos_edge": edge(oos_d),
            "cov": d["cov"].mean(), "winner_px": d["wpx"].mean(),
            "stale_min": d["stale"].median(),
        })

    print("=" * 92)
    print("CHICAGO -- is the rising edge real or a proxy artifact?  (top-2 wing, gate<0.90)")
    print("=" * 92)
    print(f"{'anchor':>7}{'fired':>7}{'IS_edge':>9}{'OOS_edge':>10}{'coverage':>10}"
          f"{'winnerPx':>10}{'stale_min':>11}")
    for r in rows:
        print(f"{r['anchor']:>7}{r['fired']:>7}{r['is_edge']:>+8.1f}c{r['oos_edge']:>+9.1f}c"
              f"{r['cov']:>10.0%}{r['winner_px']:>10.2f}{r['stale_min']:>11.0f}")
    print("\nREAD: if IS_edge rises but OOS_edge does NOT track it, the late rise is overfit/noise.")
    print("If coverage -> ~100% and winnerPx climbs, the 'edge' is reading an already-known answer.")
    print("If stale_min grows, the +1c ask is set off an old trade on a thin book -> not a real fill.")
    print("The honest takeaway: the edge METRIC is right (EV/$1); its COST input (+1c proxy) is the")
    print("weak link and degrades intraday -- which is why 1 PM (liquid, outcome still uncertain) is")
    print("the trustworthy anchor, and why losing cities being NEGATIVE at 1 PM is the real verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
