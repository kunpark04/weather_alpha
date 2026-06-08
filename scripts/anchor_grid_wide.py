"""Per-city edge by anchor, 8:00-16:00 local (30-min steps), for TWO wing variants -- NO pooling.

  TOP-2 wing = production: modal + higher-ask adjacent (drop_lower_ask), 2 legs.
  TOP-3 wing = full coverage: modal + BOTH adjacents (no drop), 3 legs.
Both gated at sum_asks < 0.90 and sized flat $2.50, over each city's full 3.4yr history. Each
row is ONE city; there are no totals/averages across cities (individual basis only).

Edge = coverage - cost - fee (cents per $1 payout). Reading a single city's row left->right shows
its intraday edge profile: thin/uninformative early morning (sparse books) -> a genuine midday
forecast region -> a late-afternoon RISE that is realized-high LEAKAGE (the daily max is in by
then, so the wing is pricing a known outcome; see edge_decay_example.py). The 15:00-16:00 cells are
NOT tradeable. Snapshot is computed once per (day,anchor) and shared by both variants.
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
from weather_alpha.fees import trade_fee_cents
from weather_alpha.strategy import run_wing_strategy

ANCHORS = [(h, m) for h in range(8, 17) for m in (0, 30)][:17]    # 8:00 .. 16:00
GATE = 0.90
FEE1 = lambda a: 0.07 * a * (1 - a)


def alab(a):
    h, m = a
    return f"{(h - 12) or 12 if h >= 12 else h}{':' + str(m).zfill(2) if m else ''}{'p' if h >= 12 else 'a'}"


def evaluate(contracts, winner, ev, cfg, drop_lower):
    out = run_wing_strategy(
        cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
        require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
        flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
        drop_lower_ask=drop_lower, drop_higher_ask=False, max_ask_sum=GATE)
    if not out.targets:
        return None
    legs, cov, pnl = [], 0, 0
    for tgt in out.targets:
        c = next((x for x in contracts if x.ticker == tgt.ticker), None)
        if c is None:
            continue
        a, n = c.yes_ask, tgt.target_contracts
        won = tgt.ticker == winner
        pnl += (n * 100 if won else 0) - round(a * 100) * n - trade_fee_cents(a, n)
        legs.append(a); cov = cov or int(won)
    return {"cov": cov, "cost": sum(legs), "fee": sum(FEE1(a) for a in legs), "pnl": pnl / 100.0}


def city_grid(df, tz, cfg):
    acc = {a: {"npre": 0, "t2": [], "t3": []} for a in ANCHORS}
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        winner = w[0]
        for a in ANCHORS:
            anchor = pd.Timestamp(ev.year, ev.month, ev.day, a[0], a[1], tz=tz).tz_convert("UTC")
            contracts = _snapshot(day, anchor)
            if len(contracts) < 2:
                continue
            acc[a]["npre"] += 1
            r2 = evaluate(contracts, winner, ev, cfg, True)
            r3 = evaluate(contracts, winner, ev, cfg, False)
            if r2:
                acc[a]["t2"].append(r2)
            if r3:
                acc[a]["t3"].append(r3)
    out = {}
    for a, d in acc.items():
        out[a] = {"npre": d["npre"]}
        for k in ("t2", "t3"):
            xs = d[k]
            if xs:
                cov = np.mean([x["cov"] for x in xs])
                cost = np.mean([x["cost"] for x in xs])
                fee = np.mean([x["fee"] for x in xs])
                out[a][k] = {"fired": len(xs), "edge": cov - cost - fee, "cov": cov}
            else:
                out[a][k] = {"fired": 0, "edge": np.nan, "cov": np.nan}
    return out


def emit(grids, variant, label):
    print("\n" + "=" * 128)
    print(f"{label}  --  EDGE (cents/$1 = coverage-cost-fee) by anchor, per city.  8-11a thin/early; "
          f"3-4p = realized-high LEAKAGE, not tradeable")
    print("=" * 128)
    print(f"{'city':<14}" + "".join(alab(a).rjust(6) for a in ANCHORS))
    for s in grids:
        cells = ""
        for a in ANCHORS:
            e = grids[s][a][variant]["edge"]
            cells += f"{'-':>6}" if (e is None or np.isnan(e)) else f"{e*100:>+6.1f}"
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

    emit(grids, "t2", "TOP-2 WING (production: modal + higher-ask adjacent, drop_lower_ask)")
    emit(grids, "t3", "TOP-3 WING (full: modal + both adjacents, no drop)")

    # top-3 fire% (it fires less -- 3 buckets rarely sum < 0.90)
    print("\n" + "=" * 128)
    print("TOP-3 fire% by anchor (full wing rarely clears the 0.90 gate -> sparse) ; per city")
    print("=" * 128)
    print(f"{'city':<14}" + "".join(alab(a).rjust(6) for a in ANCHORS))
    for s in grids:
        cells = ""
        for a in ANCHORS:
            d = grids[s][a]
            f = d["t3"]["fired"] / d["npre"] if d["npre"] else np.nan
            cells += f"{'-':>6}" if np.isnan(f) else f"{f:>5.0%} "
        print(f"{CITY[s][0]:<14}{cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
