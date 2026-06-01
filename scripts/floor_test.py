"""Test the proposed gate refinement: floor sum_asks at 0.85 (buy only .85-.90), vs the
current baseline (buy all sum < 0.90). HIGH temp, 1 PM, fee-aware.

CAVEAT built in: 0.85 was eyeballed from the full window, so the in-sample comparison is
circular (the floor removes exactly the bands just labeled negative -> it MUST look better
in-sample). The verdict is the OUT-OF-SAMPLE block: apply the fixed 0.85 floor to the
held-out second half (chronological per-city split) and see if it still beats baseline.
Per-city floor fire counts show whether the idea is even testable at the city level.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from best_anchor import day_net, STAKE  # noqa: E402


def bt(g):
    g = g.sort_values("date")
    nets = [day_net(s, m, c)[0] for s, m, c in
            zip(g["wing_sum_asks"], g["modal_ask"], g["wing_covers"])]
    n = len(nets)
    cum = peak = dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return {"fires": n, "win": g["wing_covers"].mean() if n else float("nan"),
            "pnl": sum(nets), "netf": sum(nets) / n * 100 if n else float("nan"),
            "roi": sum(nets) / (n * STAKE) if n else float("nan"), "dd": dd}


def show(df, label):
    base = bt(df[(df["wing_sum_asks"] > 0) & (df["wing_sum_asks"] < 0.90)])
    flr = bt(df[(df["wing_sum_asks"] >= 0.85) & (df["wing_sum_asks"] < 0.90)])
    print(f"{label}")
    for name, r in [("baseline <0.90", base), ("floor .85-.90 ", flr)]:
        w = "  -- " if r["win"] != r["win"] else f"{r['win']:.0%}"
        nf = "  -- " if r["netf"] != r["netf"] else f"{r['netf']:+.1f}"
        ro = "  -- " if r["roi"] != r["roi"] else f"{r['roi']:+.1%}"
        print(f"   {name}: fires {r['fires']:>3}  win {w:>5}  PnL {r['pnl']:>+7.2f}  "
              f"net/f {nf:>6}c  ROI {ro:>7}  maxDD {r['dd']:>5.2f}")
    print()


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    hi = df[(df["kind"] == "high") & (df["hour"] == 13)]

    print("=" * 70)
    print("IN-SAMPLE (full window, 1 PM)  — circular; expect floor to win by construction")
    print("=" * 70)
    show(hi, "ALL high cities pooled")
    show(hi[hi["city"] == "Chicago"], "Chicago only")

    parts = []
    for city, g in hi.groupby("city"):
        dates = sorted(g["date"].unique())
        parts.append(g[g["date"].isin(set(dates[len(dates) // 2:]))])
    oos = pd.concat(parts)
    print("=" * 70)
    print("OUT-OF-SAMPLE (held-out 2nd half, 1 PM)  — the actual test")
    print("=" * 70)
    show(oos, "ALL high cities pooled (OOS half)")
    show(oos[oos["city"] == "Chicago"], "Chicago only (OOS half)")

    print("=" * 70)
    print("floor .85-.90 fire counts per city (full window, 1 PM) — is it even testable?")
    print("=" * 70)
    for city, g in sorted(hi.groupby("city"), key=lambda kv: -len(kv[1][(kv[1]['wing_sum_asks'] >= 0.85) & (kv[1]['wing_sum_asks'] < 0.90)])):
        f = g[(g["wing_sum_asks"] >= 0.85) & (g["wing_sum_asks"] < 0.90)]
        print(f"   {city:<15} {len(f):>3} fires")


if __name__ == "__main__":
    main()
