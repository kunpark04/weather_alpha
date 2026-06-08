"""Why does the realistic edge drop at 1 PM (where prices are FRESH, so it's not staleness)?

Decomposes the proxy->realistic edge drop at the 1 PM anchor into its two parts, per city:
  (1) SPREAD/price-move: the +1c proxy assumes a 1c spread, but to actually BUY each leg you cross
      the real bid-ask; the next trade clears higher. Measured on the SAME (fillable) days, so this
      isolates the price effect: proxy_edge(fillable) -> real_edge(fillable).
  (2) SELECTION: the realistic number is computed only on days both legs retrade within 60 min;
      those days can have a different coverage than all fired days: proxy_edge(all) -> proxy_edge(fillable).

Per leg it also prints last_price (what the proxy is built on), proxy_ask (last+1c), and next_trade
(the realistic fill), so the real half-spread = next_trade - last is visible vs the proxy's assumed 1c.
Chicago / Miami / NYC at 1 PM, top-2 wing, gate<0.90.
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
from scripts.modal_rank_multicity import CITY, load_city
from weather_alpha.config import load_config
from weather_alpha.strategy import run_wing_strategy

WINDOW = pd.Timedelta(minutes=60)
FEE1 = lambda a: 0.07 * a * (1 - a)


def decomp(series, cfg):
    df = load_city(series)
    tz = ZoneInfo(CITY[series][1])
    recs = []
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        winner = w[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        anchor = pd.Timestamp(ev.year, ev.month, ev.day, 13, tz=tz).tz_convert("UTC")
        contracts = _snapshot(day, anchor)
        if len(contracts) < 2:
            continue
        res = run_wing_strategy(
            cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
            require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
            flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
            drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.90)
        if not res.targets:
            continue
        legs = [t.ticker for t in res.targets]
        cmap = {c.ticker: c for c in contracts}
        last = [cmap[tk].last_price for tk in legs]            # mid the proxy is built on
        proxy = [cmap[tk].yes_ask for tk in legs]              # last + 1c
        cov = int(winner in legs)
        fut = day[(day["timestamp"] > anchor) & (day["timestamp"] <= anchor + WINDOW)]
        real, ok = [], True
        for tk in legs:
            tr = fut[fut["ticker"] == tk]["yes_price_cents"]
            if len(tr):
                real.append(tr.iloc[0] / 100.0)
            else:
                ok = False
                break
        recs.append({"cov": cov, "last": sum(last), "proxy": sum(proxy),
                     "pfee": sum(FEE1(x) for x in proxy),
                     "real": sum(real) if ok else np.nan,
                     "rfee": sum(FEE1(x) for x in real) if ok else np.nan,
                     "fill": ok,
                     "movemax": (max(real) - max(last)) if ok else np.nan})
    d = pd.DataFrame(recs)
    f = d[d["fill"]]
    return {
        "n": len(d), "nf": len(f), "fillpct": len(f) / len(d),
        "cov_all": d["cov"].mean(), "cov_f": f["cov"].mean(),
        "pcost_all": d["proxy"].mean(), "pcost_f": f["proxy"].mean(), "rcost_f": f["real"].mean(),
        "last_f": f["last"].mean(),
        "edge_p_all": (d["cov"].mean() - d["proxy"].mean() - d["pfee"].mean()) * 100,
        "edge_p_f": (f["cov"].mean() - f["proxy"].mean() - f["pfee"].mean()) * 100,
        "edge_r_f": (f["cov"].mean() - f["real"].mean() - f["rfee"].mean()) * 100,
    }


def main():
    cfg = load_config()
    print("=" * 100)
    print("WHY THE 1 PM EDGE DROPS: spread (price you cross) vs selection (fillable subset)")
    print("=" * 100)
    for s in ["KXHIGHCHI", "KXHIGHMIA", "KXHIGHNY"]:
        r = decomp(s, cfg)
        name = CITY[s][0]
        print(f"\n## {name}   fired={r['n']}  fillable={r['nf']} ({r['fillpct']:.0%})")
        print(f"   wing cost (sum of 2 legs):  last-mid {r['last_f']:.3f}  ->  proxy ask(+1c) "
              f"{r['pcost_f']:.3f}  ->  realistic fill {r['rcost_f']:.3f}")
        print(f"     proxy assumes a {100*(r['pcost_f']-r['last_f']):.1f}c spread on the wing; "
              f"the realistic fill is {100*(r['rcost_f']-r['pcost_f']):+.1f}c ABOVE the proxy "
              f"(~{100*(r['rcost_f']-r['pcost_f'])/2:.1f}c per leg of true spread/move).")
        print(f"   coverage:  all-fired {r['cov_all']:.0%}   fillable {r['cov_f']:.0%}")
        print(f"   EDGE walk:")
        print(f"     proxy, all fired days      : {r['edge_p_all']:>+6.1f}c")
        print(f"     proxy, fillable days only  : {r['edge_p_f']:>+6.1f}c   "
              f"(selection: {r['edge_p_f']-r['edge_p_all']:+.1f}c)")
        print(f"     REALISTIC, fillable days   : {r['edge_r_f']:>+6.1f}c   "
              f"(spread:    {r['edge_r_f']-r['edge_p_f']:+.1f}c)")
    print("\nThe SPREAD step (proxy->realistic on the SAME days) is the real cost of buying: you cross")
    print("the bid-ask on BOTH legs. It is present at EVERY anchor, including 1 PM where staleness is ~0.")
    print("Staleness is a SEPARATE, additional tax that only bites later in the day.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
