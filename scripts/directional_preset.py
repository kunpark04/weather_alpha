"""Runnable presets for the two LOCKED directional configs (2026-06-15, report §6f).

  lowtemp22  : LOW-temp, 22:00 local anchor, favorite price band [0.93,0.95], top-3 cities, 50% of
               account split evenly across the picks, hold to settlement, compound.
  hightemp17 : HIGH-temp, 17:00 local anchor, same band / top-3 / 50%-split rule.

Both run the backtest on the existing per-event tables and print the metrics. This is a BACKTEST/sim
preset (last-trade prices -> optimistic ceiling; NOT live-validated -- see report caveats). Forward
deployment against live asks is a separate step.

Usage:
  python scripts/directional_preset.py --preset hightemp17 [--bankroll 250] [--throttle]
  python scripts/directional_preset.py --preset lowtemp22
  python scripts/directional_preset.py --list
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
import numpy as np, pandas as pd

_ROOT = Path(__file__).resolve().parent.parent

PRESETS = {
    "lowtemp22":  dict(market="LOW-temp",  anchor="22:00", band=(0.93, 0.95), ncities=3, stake=0.50,
                       data="data/lowtemp_anchor_events.parquet", col="anchor", since=None),
    "hightemp17": dict(market="HIGH-temp", anchor="17:00", band=(0.93, 0.95), ncities=3, stake=0.50,
                       data="data/hightemp_aft_events.parquet",   col="snap",  since="2026-01-01"),
}


def run(p, bankroll, throttle):
    fp = _ROOT / p["data"]
    if not fp.exists():
        print(f"missing data: {fp} (build it first — see report §6f reproduce)"); return 1
    df = pd.read_parquet(fp); df["date"] = pd.to_datetime(df["date"])
    df = df[df[p["col"]] == p["anchor"]]
    if p["since"]:
        df = df[df["date"] >= pd.Timestamp(p["since"])]
    a, b = p["band"]; nc = p["ncities"]; stake = p["stake"]
    bal = bankroll; eq = [bal]; daily = []; pk = wn = am = 0; pp = []
    for d, day in df.sort_values("date").groupby("date"):
        cand = day[(day.price >= a) & (day.price <= b)].sort_values("price", ascending=False)
        if cand.empty:
            continue
        chosen = [r for _, r in cand.head(nc).iterrows()]; n = len(chosen); pp.append(n)
        if all(not c.win for c in chosen):
            am += 1
        per = (stake / nc) if throttle else (stake / n)
        tot = per * n; pay = 0.0
        for c in chosen:
            pay += per * bal / (c.price * (1 + 0.07 * (1 - c.price))) if c.win else 0.0
            pk += 1; wn += int(c.win)
        bal = bal - tot * bal + pay; daily.append(bal - eq[-1]); eq.append(bal)
    eq = np.array(eq); daily = np.array(daily); peak = np.maximum.accumulate(eq); nn = max(1, len(daily))
    dd = float((eq / peak - 1).min()) if len(eq) > 1 else 0.0
    print(f"\n=== PRESET {p['market']} @ {p['anchor']} | band {a}-{b} | top-{nc} | "
          f"{'16.7%/city throttle' if throttle else '50% split'} | ${bankroll:.0f} ===")
    print(f"  nights={nn}  avg picks/night={np.mean(pp):.1f}  hit={wn/pk:.1%}  all-miss nights={am}")
    print(f"  end balance ${bal:.0f}   PnL ${bal-bankroll:+.0f}   avg ${ (bal-bankroll)/nn:+.2f}/day")
    print(f"  max DD {dd:.0%} (${float((peak-eq).max()):.0f})   worst night ${daily.min():+.0f}")
    print(f"  NOTE: last-trade prices = optimistic ceiling; not live-validated (report §6f caveats).")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=list(PRESETS))
    ap.add_argument("--bankroll", type=float, default=250.0)
    ap.add_argument("--throttle", action="store_true", help="16.7%%/city (lower DD) instead of 50%% split")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args(argv)
    if a.list or not a.preset:
        print("presets:", ", ".join(PRESETS));
        for k, v in PRESETS.items():
            print(f"  {k}: {v['market']} @ {v['anchor']}, band {v['band']}, top-{v['ncities']}, {v['stake']:.0%} split")
        return 0
    return run(PRESETS[a.preset], a.bankroll, a.throttle)


if __name__ == "__main__":
    sys.exit(main())
