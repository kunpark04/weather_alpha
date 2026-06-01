"""Does the wing's edge grow as sum_asks falls (so we'd 'bet more when sum << 1')?

Tests the premise: fee-aware net/fire by sum_asks band, plus a direct A/B of flat $2.50
vs a 'bet-more-when-cheaper' rule (stake ∝ 0.90 - sum, same average stake) on the
affordable days. If cheap wings have a smaller/negative edge, betting more there HURTS.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from weather_alpha.fees import trade_fee_cents  # noqa: E402

AFFORD = 0.90
BINS = [0, .70, .75, .80, .85, .90, .95, 1.00, 9]
LBL = ["<.70", ".70-.75", ".75-.80", ".80-.85", ".85-.90", ".90-.95", ".95-1.0", ">1.0"]


def net_for_stake(sum_asks, modal_ask, covered, stake):
    K = max(1, round(stake / sum_asks))
    a_m, a_j = modal_ask, max(0.0, sum_asks - modal_ask)
    fee = (trade_fee_cents(a_m, K) + trade_fee_cents(a_j, K)) / 100.0
    return (K if covered else 0.0) - K * sum_asks - fee


def band_table(df, title):
    b = pd.cut(df["wing_sum_asks"], BINS, labels=LBL, right=False)
    print(title)
    print(f"{'band':<9}{'n':>5}{'win%':>6}{'cost':>6}{'gap':>7}{'net/f¢':>8}")
    for lbl in LBL:
        g = df[b == lbl]
        if g.empty:
            continue
        nets = [net_for_stake(s, m, c, 2.50) for s, m, c in
                zip(g["wing_sum_asks"], g["modal_ask"], g["wing_covers"])]
        win, cost = g["wing_covers"].mean(), g["wing_sum_asks"].mean()
        print(f"{lbl:<9}{len(g):>5}{win:>6.0%}{cost:>6.2f}{(win - cost) * 100:>+7.1f}"
              f"{sum(nets) / len(nets) * 100:>+8.1f}")
    print()


def ab_test(df, title):
    aff = df[df["wing_sum_asks"] < AFFORD]
    if aff.empty:
        print(f"{title}: no affordable fires")
        return
    flat = [net_for_stake(s, m, c, 2.50) for s, m, c in
            zip(aff["wing_sum_asks"], aff["modal_ask"], aff["wing_covers"])]
    w = (0.90 - aff["wing_sum_asks"]).clip(lower=0.01)
    w = w / w.mean() * 2.50                                   # same average stake as flat
    wt = [net_for_stake(s, m, c, st) for s, m, c, st in
          zip(aff["wing_sum_asks"], aff["modal_ask"], aff["wing_covers"], w)]
    print(f"{title}  ({len(flat)} fires, same avg stake $2.50):")
    print(f"    flat $2.50               total {sum(flat):+7.2f}$   ({sum(flat)/len(flat)*100:+.1f}c/fire)")
    print(f"    bet-more-when-cheaper    total {sum(wt):+7.2f}$   ({sum(wt)/len(wt)*100:+.1f}c/fire)")
    print()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    df = pd.read_parquet("data/accuracy_all.parquet")
    hi13 = df[(df["kind"] == "high") & (df["hour"] == 13)]
    band_table(hi13, "Fee-aware net/fire by sum band — ALL high cities @ 1 PM")
    band_table(hi13[hi13["city"] == "Chicago"], "Fee-aware net/fire by sum band — CHICAGO @ 1 PM")
    print("A/B: flat vs 'bet more when sum is lower' (stake ∝ 0.90 − sum)")
    print("-" * 64)
    ab_test(hi13, "ALL high cities @ 1 PM")
    ab_test(hi13[hi13["city"] == "Chicago"], "CHICAGO @ 1 PM")


if __name__ == "__main__":
    main()
