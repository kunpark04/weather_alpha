"""Out-of-sample validation of the best-anchor wing edge (HIGH temp), fee-aware.

The best-anchor backtest was selection-biased (best-of-6 anchors x 10 cities, scored on the
full window). This tests honestly:

  TEST 1 (anchor-selection OOS): split each city's days CHRONOLOGICALLY at the midpoint.
    Pick the best anchor on the FIRST half (in-sample) only, then score the held-out SECOND
    half at that anchor with FIXED equal-payout, no parameter search. A real edge survives
    OOS; an overfit one collapses.

  TEST 2 (pre-specified anchor): fix anchor = 1 PM for every city (the production choice,
    zero mining) over the full window. Directly tests "does the edge exist without anchor
    selection at all."

Single 50/50 time split (not k-fold) — ~30 days/half is thin; read with that caveat.
Reuses best_anchor.day_net (real Kalshi fees, $2.50 equal-payout).
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from best_anchor import day_net, STAKE, AFFORD  # noqa: E402

HOURS = [8, 9, 10, 11, 12, 13]
MIN_IS = 5            # min in-sample fires for an anchor to be selectable
PRESPEC = 13          # production pre-specified anchor (1 PM)


def net_of(sub):
    """(affordable_df, [net per day]) fee-aware for a city+hour slice."""
    aff = sub[sub["wing_sum_asks"] < AFFORD]
    nets = [day_net(s, m, c)[0] for s, m, c in
            zip(aff["wing_sum_asks"], aff["modal_ask"], aff["wing_covers"])]
    return aff, nets


def max_drawdown(nets):
    cum = peak = dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def stats(aff, nets):
    return {"fires": len(nets), "pnl": sum(nets),
            "roi": (sum(nets) / (len(nets) * STAKE) if nets else 0.0),
            "wr": (aff["wing_covers"].mean() if len(aff) else float("nan")),
            "maxdd": max_drawdown(nets)}


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    df = df[df["kind"] == "high"].copy()

    # ---------- TEST 1: IS-select anchor, OOS-test ----------
    rows = []
    for city, g in df.groupby("city"):
        dates = sorted(g["date"].unique())
        mid = len(dates) // 2
        gis = g[g["date"].isin(set(dates[:mid]))]
        goos = g[g["date"].isin(set(dates[mid:]))]
        best_h, best_tot = None, -1e18
        for h in HOURS:                                   # select on IS (>= MIN_IS fires)
            _, nets = net_of(gis[gis["hour"] == h])
            if len(nets) >= MIN_IS and sum(nets) > best_tot:
                best_tot, best_h = sum(nets), h
        if best_h is None:                                # fallback: argmax regardless
            for h in HOURS:
                _, nets = net_of(gis[gis["hour"] == h])
                if nets and sum(nets) > best_tot:
                    best_tot, best_h = sum(nets), h
        si = stats(*net_of(gis[gis["hour"] == best_h]))
        so = stats(*net_of(goos[goos["hour"] == best_h]))
        rows.append({"city": city, "is_n": mid, "oos_n": len(dates) - mid, "anchor": best_h,
                     "is_fires": si["fires"], "is_pnl": si["pnl"], "oos_fires": so["fires"],
                     "oos_pnl": so["pnl"], "oos_roi": so["roi"], "oos_wr": so["wr"],
                     "oos_dd": so["maxdd"]})
    t1 = pd.DataFrame(rows).sort_values("oos_pnl", ascending=False)

    print("=" * 92)
    print("TEST 1 — anchor chosen IN-SAMPLE (first half), scored OUT-OF-SAMPLE (held-out second half)")
    print("=" * 92)
    print(f"{'city':<15}{'anchor':>7}{'isFire':>7}{'isPnL':>8}  |{'oosFire':>8}{'oosPnL':>8}"
          f"{'oosROI':>8}{'oosWR':>7}{'oosDD':>7}  verdict")
    for _, r in t1.iterrows():
        if r["oos_fires"] < 5:
            v = "no OOS fires"
        elif r["oos_pnl"] <= 0:
            v = "FAILS OOS"
        elif r["oos_fires"] < 10:
            v = "holds? (thin)"
        else:
            v = "HOLDS"
        wr = "  -- " if r["oos_wr"] != r["oos_wr"] else f"{r['oos_wr']:4.0%}"
        print(f"{r['city']:<15}{r['anchor']:>5}:00{r['is_fires']:>7}{r['is_pnl']:>+8.2f}  |"
              f"{r['oos_fires']:>8}{r['oos_pnl']:>+8.2f}{r['oos_roi']:>+8.1%}{wr:>7}{r['oos_dd']:>7.2f}  {v}")
    print(f"\nAggregate OOS (each city at its IS-selected anchor): {int(t1['oos_fires'].sum())} fires, "
          f"net {t1['oos_pnl'].sum():+.2f}$  |  IS aggregate was {t1['is_pnl'].sum():+.2f}$ over {int(t1['is_fires'].sum())} fires.")

    # ---------- TEST 2: pre-specified 1 PM, full window (no mining) ----------
    rows2 = []
    for city, g in df.groupby("city"):
        s = stats(*net_of(g[g["hour"] == PRESPEC]))
        rows2.append({"city": city, **s})
    t2 = pd.DataFrame(rows2).sort_values("pnl", ascending=False)
    print("\n" + "=" * 92)
    print(f"TEST 2 — PRE-SPECIFIED 1 PM anchor, full window (zero anchor selection)")
    print("=" * 92)
    print(f"{'city':<15}{'fires':>6}{'PnL$':>8}{'ROI%':>8}{'WR%':>7}{'maxDD$':>8}")
    for _, r in t2.iterrows():
        wr = "  -- " if r["wr"] != r["wr"] else f"{r['wr']:4.0%}"
        print(f"{r['city']:<15}{r['fires']:>6}{r['pnl']:>+8.2f}{r['roi']:>+8.1%}{wr:>7}{r['maxdd']:>8.2f}")
    print(f"\nAggregate @1PM: {int(t2['fires'].sum())} fires, net {t2['pnl'].sum():+.2f}$.")

    t1.to_csv("data/oos_split_test1.csv", index=False)
    t2.to_csv("data/oos_split_test2.csv", index=False)
    print("\nfull -> data/oos_split_test1.csv, data/oos_split_test2.csv")


if __name__ == "__main__":
    main()
