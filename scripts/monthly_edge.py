"""Per-city 1 PM wing economics split by MONTH-OF-YEAR (seasonality) -- individual basis, no pooling.

Tests the hypothesis that the cross-city edge differences are a seasonality artifact: a city's high
may be far more forecastable in its stable season than its volatile one, and cities differ in which
months are stable. For each city x month-of-year (Jan..Dec) at the 1 PM anchor (top-2 production
wing, gate<0.90) it reports, over the 3.4yr history:
  coverage   = winner inside the 2-leg wing on fired days
  realEdge   = coverage - realistic_cost - fee   (realistic_cost = next actual trade within 60min)
  proxyEdge  = coverage - (last+1c)_cost - fee    (optimistic; shown for contrast)
  nFired     = sample behind each cell (deep cities ~30-50/month-of-yr; shallow ~0-10 = noise)
Only cities with multi-year history (Chicago/NYC/Miami/Austin/Denver/Philly/LA) populate most
months; 2026-launch cities have only a few months and cannot show seasonality (flagged by nFired).
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
MONTHS = list(range(1, 13))
MLAB = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def city_months(df, tz, cfg):
    rows = []
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
        rows.append({"month": ev.month, "cov": cov,
                     "pcost": sum(proxy), "pfee": sum(FEE1(x) for x in proxy),
                     "rcost": sum(real) if ok else np.nan, "rfee": sum(FEE1(x) for x in real) if ok else np.nan,
                     "fill": ok})
    return pd.DataFrame(rows)


def main():
    cfg = load_config()
    data = {}
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        data[s] = city_months(df, ZoneInfo(CITY[s][1]), cfg)
        print(f"  done {CITY[s][0]}", flush=True)

    order = sorted(data, key=lambda s: -len(data[s]))   # deepest first

    def cell(g, kind):
        if g.empty:
            return None
        if kind == "cov":
            return g["cov"].mean()
        if kind == "redge":
            f = g.dropna(subset=["rcost"])
            return (f["cov"].mean() - f["rcost"].mean() - f["rfee"].mean()) if len(f) else np.nan
        if kind == "pedge":
            return g["cov"].mean() - g["pcost"].mean() - g["pfee"].mean()
        if kind == "n":
            return len(g)

    def grid(kind, title, fmt):
        print("\n" + "=" * 104)
        print(title)
        print("=" * 104)
        print(f"{'city':<14}" + "".join(m.rjust(7) for m in MLAB))
        for s in order:
            cells = ""
            for m in MONTHS:
                g = data[s][data[s]["month"] == m]
                v = cell(g, kind)
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    cells += f"{'-':>7}"
                elif fmt == "pct":
                    cells += f"{v:>6.0%} "
                elif fmt == "edge":
                    cells += f"{v*100:>+7.1f}"
                else:
                    cells += f"{int(v):>7}"
            print(f"{CITY[s][0]:<14}{cells}")

    grid("n", "nFIRED per month-of-year (sample size; <10 = noisy cell)", "int")
    grid("cov", "COVERAGE by month-of-year (winner in 2-leg wing, fired days)", "pct")
    grid("redge", "REALISTIC edge by month-of-year (cents/$1; next-trade fill)", "edge")
    grid("pedge", "PROXY edge by month-of-year (cents/$1; stale last+1c -- optimistic)", "edge")
    return 0


if __name__ == "__main__":
    sys.exit(main())
