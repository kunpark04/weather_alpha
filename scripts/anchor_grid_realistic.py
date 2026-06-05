"""Realistic-fill version of the per-city anchor edge grid (top-2 production wing), 8a-16:00 / 30-min.

Replaces the stale last_trade+1c ask with the REALISTIC fill = the price of the next actual trade on
each leg within 60 min after the anchor (a market order's approximate clear). A day where any leg has
no trade in that window is UNFILLABLE (the pure liquidity signal). The wing DECISION (which 2 legs,
gate < 0.90) still uses the proxy book at the anchor, as a live bot would; only the FILL price is made
realistic.

Emits, per city (no pooling): REALISTIC edge grid, FILL% grid (liquidity), and a 1 PM table
(proxy edge vs realistic edge vs fill%). CAVEATS: next-trade price is hindsight (a realism check,
not a live signal) and mildly PESSIMISTIC for a buy (you'd cross to the ask >= it); the true ask
only exists in the forward order-book log. Edge = coverage - cost - fee, cents per $1 payout.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from scripts.backtest_multicity import _dummy_pred, _snapshot
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config
from weather_alpha.strategy import run_wing_strategy

ANCHORS = [(h, m) for h in range(8, 17) for m in (0, 30)][:17]
WINDOW = pd.Timedelta(minutes=60)
GATE = 0.90
FEE1 = lambda a: 0.07 * a * (1 - a)


def alab(a):
    h, m = a
    return f"{(h - 12) or 12 if h >= 12 else h}{':' + str(m).zfill(2) if m else ''}{'p' if h >= 12 else 'a'}"


def city_grid(df, tz, cfg):
    winners = {}
    days = {}
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) == 1:
            winners[ev] = w[0]
            days[ev] = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")

    out = {}
    for a in ANCHORS:
        cov_all, pc, pf = [], [], []
        cov_f, rc, rf = [], [], []
        nfired = nfill = 0
        for ev, winner in winners.items():
            day = days[ev]
            anchor = pd.Timestamp(ev.year, ev.month, ev.day, a[0], a[1], tz=tz).tz_convert("UTC")
            contracts = _snapshot(day, anchor)
            if len(contracts) < 2:
                continue
            res = run_wing_strategy(
                cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
                require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
                flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
                drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=GATE)
            if not res.targets:
                continue
            legs = [t.ticker for t in res.targets]
            proxy = [next(c.yes_ask for c in contracts if c.ticker == tk) for tk in legs]
            cov = int(winner in legs)
            nfired += 1
            cov_all.append(cov); pc.append(sum(proxy)); pf.append(sum(FEE1(x) for x in proxy))
            fut = day[(day["timestamp"] > anchor) & (day["timestamp"] <= anchor + WINDOW)]
            real = []
            ok = True
            for tk in legs:
                tr = fut[fut["ticker"] == tk]["yes_price_cents"]
                if len(tr):
                    real.append(tr.iloc[0] / 100.0)
                else:
                    ok = False
                    break
            if ok:
                nfill += 1
                cov_f.append(cov); rc.append(sum(real)); rf.append(sum(FEE1(x) for x in real))
        if nfired == 0:
            out[a] = {"nfired": 0, "fillpct": np.nan, "pedge": np.nan, "redge": np.nan}
            continue
        pedge = (np.mean(cov_all) - np.mean(pc) - np.mean(pf)) * 100
        redge = ((np.mean(cov_f) - np.mean(rc) - np.mean(rf)) * 100) if cov_f else np.nan
        out[a] = {"nfired": nfired, "fillpct": nfill / nfired, "pedge": pedge, "redge": redge}
    return out


def grid_table(grids, key, title, pct=False):
    print("\n" + "=" * 132)
    print(title)
    print("=" * 132)
    print(f"{'city':<14}" + "".join(alab(a).rjust(7) for a in ANCHORS))
    for s in grids:
        cells = ""
        for a in ANCHORS:
            v = grids[s][a][key]
            if v is None or (isinstance(v, float) and np.isnan(v)):
                cells += f"{'-':>7}"
            elif pct:
                cells += f"{v:>6.0%} "
            else:
                cells += f"{v:>+7.1f}"
        print(f"{CITY[s][0]:<14}{cells}")


def main():
    cfg = load_config()
    grids = {}
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        grids[s] = city_grid(df, ZoneInfo(CITY[s][1]), cfg)
        print(f"  done {CITY[s][0]}", flush=True)

    grid_table(grids, "redge", "REALISTIC edge (next-trade fill) by anchor, cents/$1 -- top-2 wing, per city")
    grid_table(grids, "fillpct", "FILL%% (of fired days, both legs trade within 60 min) by anchor, per city", pct=True)

    one = (13, 0)
    print("\n" + "=" * 80)
    print("AT 1 PM: proxy edge (stale +1c) vs REALISTIC edge (next trade) vs fill%, per city")
    print("=" * 80)
    print(f"{'city':<14}{'fired':>7}{'fill%':>7}{'proxyEdge':>11}{'realEdge':>10}")
    for s in sorted(grids, key=lambda s: -(grids[s][one]['redge'] if grids[s][one]['redge'] == grids[s][one]['redge'] else -99)):
        d = grids[s][one]
        re_ = f"{d['redge']:>+9.1f}c" if d['redge'] == d['redge'] else f"{'-':>10}"
        print(f"{CITY[s][0]:<14}{d['nfired']:>7}{d['fillpct']:>6.0%} {d['pedge']:>+10.1f}c{re_}")
    print("\nREAD: realistic edge is the proxy grid with the stale-price inflation removed. Expect the")
    print("afternoon ramp to flatten/collapse and most midday edges to thin toward ~0; fill% falls late.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
