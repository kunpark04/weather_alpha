"""Per-city decision matrix at 1 PM: cross every correction, view each city as its OWN bet.

For each city (no pooling) at the 1 PM anchor, top-2 wing, gate<0.90, over the deep history:
  fires, fires/yr, fill%            -> trade FREQUENCY (the 'large N' the compounding thesis needs;
                                       effective trades = fires/yr * fill%, since unfillable days
                                       are trades you never get on)
  coverage                          -> the win rate (settlement truth, unbiased)
  proxy edge  IS / OOS              -> OPTIMISTIC bound (last+1c ask, 100% fill)
  realistic edge IS / OOS           -> PESSIMISTIC bound (next-trade fill on fillable days; bakes in
                                       spread + adverse selection)
The TRUE per-trade edge sits between the two bounds. A city is a real bet only if it clears 0 in the
PESSIMISTIC column AND holds OOS. Also prints proxy edge by YEAR (deep cities) to check whether the
edge is decaying as the market matures (a stationarity assumption).
Edge = coverage - cost - fee, cents per $1 payout. Fee = 0.07*p*(1-p)/leg (continuous approx; the
live PnL uses the ceil() fee, slightly higher at small N -> the edges here are a touch optimistic).
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
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config
from weather_alpha.strategy import run_wing_strategy

WINDOW = pd.Timedelta(minutes=60)
FEE1 = lambda a: 0.07 * a * (1 - a)
SPLIT = 0.80


def per_day(df, tz, cfg):
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
        proxy = [next(c.yes_ask for c in contracts if c.ticker == tk) for tk in legs]
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
        recs.append({"date": ev, "year": ev.year, "cov": cov,
                     "pcost": sum(proxy), "pfee": sum(FEE1(x) for x in proxy),
                     "rcost": sum(real) if ok else np.nan,
                     "rfee": sum(FEE1(x) for x in real) if ok else np.nan, "fill": ok})
    return pd.DataFrame(recs)


def pedge(d):
    return (d["cov"].mean() - d["pcost"].mean() - d["pfee"].mean()) * 100 if len(d) else np.nan


def redge(d):
    f = d[d["fill"]]
    return (f["cov"].mean() - f["rcost"].mean() - f["rfee"].mean()) * 100 if len(f) else np.nan


def main():
    cfg = load_config()
    rows, trend = [], {}
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        d = per_day(df, ZoneInfo(CITY[s][1]), cfg)
        if d.empty:
            continue
        dates = sorted(d["date"].unique())
        span_yr = max((dates[-1] - dates[0]).days / 365.0, 0.25)
        cut = dates[int(len(dates) * SPLIT)]
        is_d, oos_d = d[d["date"] < cut], d[d["date"] >= cut]
        rows.append({
            "city": CITY[s][0], "fired": len(d), "firesYr": len(d) / span_yr,
            "fillpct": d["fill"].mean(), "effYr": len(d) / span_yr * d["fill"].mean(),
            "cov": d["cov"].mean(),
            "p_is": pedge(is_d), "p_oos": pedge(oos_d),
            "r_is": redge(is_d), "r_oos": redge(oos_d),
        })
        trend[CITY[s][0]] = {y: pedge(d[d["year"] == y]) for y in (2023, 2024, 2025, 2026)}

    rows.sort(key=lambda r: -(r["r_is"] if r["r_is"] == r["r_is"] else -99))
    print("=" * 116)
    print("PER-CITY @ 1 PM  -- optimistic (proxy) vs pessimistic (realistic) edge, IS & OOS, with frequency")
    print("=" * 116)
    print(f"{'city':<14}{'fired':>6}{'fires/yr':>9}{'fill%':>7}{'effYr':>7}{'cover':>7}"
          f"{'pIS':>7}{'pOOS':>7}{'rIS':>7}{'rOOS':>7}")
    for r in rows:
        def c(x):
            return f"{x:>+7.1f}" if x == x else f"{'-':>7}"
        print(f"{r['city']:<14}{r['fired']:>6}{r['firesYr']:>9.0f}{r['fillpct']:>6.0%} "
              f"{r['effYr']:>6.0f}{r['cov']:>7.0%}{c(r['p_is'])}{c(r['p_oos'])}{c(r['r_is'])}{c(r['r_oos'])}")

    print("\nPROXY edge by YEAR (deep cities; is the edge decaying as the market matures?)")
    print(f"{'city':<14}{'2023':>8}{'2024':>8}{'2025':>8}{'2026':>8}")
    for city in ["Chicago", "NYC", "Miami", "Austin", "Denver", "Philadelphia", "Los Angeles"]:
        t = trend.get(city, {})
        cells = "".join(f"{t.get(y):>+8.1f}" if t.get(y) == t.get(y) else f"{'-':>8}" for y in (2023, 2024, 2025, 2026))
        print(f"{city:<14}{cells}")
    print("\nKEY: pIS/pOOS = proxy (optimistic); rIS/rOOS = realistic (pessimistic). A real bet clears 0")
    print("in rOOS. effYr = fires/yr * fill% = the trades you actually get (the compounding 'N').")
    return 0


if __name__ == "__main__":
    sys.exit(main())
