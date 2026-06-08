"""Is the late-anchor edge REAL-but-unfillable, or just MIS-MEASURED by a stale price? Chicago.

Two competing readings of the rising late edge:
  (A) real edge you can't access -> a LIQUIDITY problem (few fills, but the price is right)
  (B) the edge is overstated because last_trade+1c is STALE on a thin book; the true price has
      moved -> a MEASUREMENT problem (at the real price the edge is smaller/gone)
They differ in what re-pricing at a REALISTIC fill does:
  * if edge SURVIVES realistic fills but few days are fillable -> (A) liquidity.
  * if edge COLLAPSES at realistic fills -> (B) the late edge was never really there.

Realistic fill = the price of the NEXT actual trade on that leg within 60 min after the anchor
(what the market truly cleared at right after you'd have decided), vs the proxy (last+1c). If a leg
has no trade within 60 min, the wing is UNFILLABLE that day (that is the pure liquidity signal).
Strategy DECISION (which 2 legs, gate<0.90) uses the proxy book, as live would; only the FILL price
is made realistic. Chicago, top-2 wing, 8:00-16:00 / 30-min.
NOTE: next-trade price is still mildly optimistic for a BUY (you'd cross to the ask >= it), so if the
late edge collapses even here, the (B) reading is strongly supported.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import _dummy_pred, _snapshot
from scripts.modal_rank_multicity import load_city
from weather_alpha.config import load_config
from weather_alpha.strategy import run_wing_strategy

CT = ZoneInfo("America/Chicago")
ANCHORS = [(h, m) for h in range(8, 17) for m in (0, 30)][:17]
FEE1 = lambda a: 0.07 * a * (1 - a)
WINDOW = pd.Timedelta(minutes=60)


def alab(a):
    h, m = a
    return f"{(h - 12) or 12 if h >= 12 else h}:{m:02d}{'p' if h >= 12 else 'a'}"


def main():
    cfg = load_config()
    df = load_city("KXHIGHCHI")
    # pre-sort once per day for next-trade lookups
    days = {ev: d.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
            for ev, d in df.groupby("event_date")}
    winners = {}
    for ev, d in df.groupby("event_date"):
        w = d.loc[d["settlement_value"] == "yes", "ticker"].unique()
        if len(w) == 1:
            winners[ev] = w[0]

    rows = []
    for a in ANCHORS:
        recs = []
        for ev, winner in winners.items():
            day = days[ev]
            anchor = pd.Timestamp(ev.year, ev.month, ev.day, a[0], a[1], tz=CT).tz_convert("UTC")
            contracts = _snapshot(day, anchor)
            if len(contracts) < 2:
                continue
            out = run_wing_strategy(
                cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
                require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
                flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
                drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.90)
            if not out.targets:
                continue
            legs = [t.ticker for t in out.targets]
            proxy = [next(c.yes_ask for c in contracts if c.ticker == tk) for tk in legs]
            cov = int(winner in legs)
            # realistic fill = first trade on each leg in (anchor, anchor+60m]
            fut = day[(day["timestamp"] > anchor) & (day["timestamp"] <= anchor + WINDOW)]
            real = []
            for tk in legs:
                tr = fut[fut["ticker"] == tk]
                real.append(tr["yes_price_cents"].iloc[0] / 100.0 if len(tr) else None)
            fillable = all(r is not None for r in real)
            recs.append({"cov": cov, "pc": sum(proxy), "pf": sum(FEE1(x) for x in proxy),
                         "rc": sum(real) if fillable else np.nan,
                         "rf": sum(FEE1(x) for x in real) if fillable else np.nan,
                         "fillable": fillable})
        if not recs:
            continue
        d = pd.DataFrame(recs)
        f = d[d["fillable"]]
        proxy_edge = (d["cov"].mean() - d["pc"].mean() - d["pf"].mean()) * 100
        real_edge = ((f["cov"].mean() - f["rc"].mean() - f["rf"].mean()) * 100) if len(f) else np.nan
        rows.append({"anchor": alab(a), "fired": len(d), "fillpct": d["fillable"].mean(),
                     "proxy_edge": proxy_edge, "real_edge": real_edge,
                     "proxy_cost": d["pc"].mean(), "real_cost": f["rc"].mean() if len(f) else np.nan})

    print("=" * 92)
    print("CHICAGO -- late edge: real-but-unfillable (liquidity) OR mis-measured (stale price)?")
    print("  proxy = last_trade+1c ; realistic = next actual trade within 60 min after the anchor")
    print("=" * 92)
    print(f"{'anchor':>7}{'fired':>7}{'fillable%':>10}{'proxyCost':>10}{'realCost':>10}"
          f"{'proxyEdge':>11}{'realEdge':>10}")
    for r in rows:
        re_ = f"{r['real_edge']:>+9.1f}c" if r["real_edge"] == r["real_edge"] else f"{'-':>10}"
        rc_ = f"{r['real_cost']:>10.3f}" if r["real_cost"] == r["real_cost"] else f"{'-':>10}"
        print(f"{r['anchor']:>7}{r['fired']:>7}{r['fillpct']:>9.0%}{r['proxy_cost']:>10.3f}{rc_}"
              f"{r['proxy_edge']:>+10.1f}c{re_}")

    print("\nVERDICT: compare realEdge to proxyEdge across the afternoon.")
    print("  realEdge stays high + fillable% stays high  -> edge is real (liquidity not the issue).")
    print("  realEdge stays high + fillable% DROPS late   -> real edge, blocked by liquidity (your read).")
    print("  realEdge COLLAPSES vs proxyEdge              -> the late edge was a stale-price artifact;")
    print("                                                  realCost > proxyCost is the price having moved.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
