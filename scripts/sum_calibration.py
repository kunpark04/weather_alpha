"""Is the 2-leg wing's price (sum_asks) a confidence signal, and is that confidence
justified by realized accuracy? Bins every HIGH-temp (city,day,hour) row by wing_sum_asks
and reports realized accuracy to settlement.

sum_asks = ask(modal) + ask(higher-ask adjacent). Each ask ~= implied prob, so a high sum
means the two central buckets carry most of the mass = a peaked / confident distribution
(plus some bid-ask overround). The question: does higher sum => higher realized coverage?

Accuracy columns (realized vs settled winner, ALL days in the bin — not just affordable):
  fav%  = favorite (modal) bucket won            (top1)
  wing% = winner inside the 2-leg wing           (wing_covers)  <- the headline
  pm1%  = winner within favorite +/-1 (3 buckets)(top3)
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

EDGES = [0.0, 0.70, 0.80, 0.90, 1.00, 1.10, 9.0]
LBL = ["<0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00", "1.00-1.10", ">=1.10"]


def report(df, title):
    n_tot = len(df)
    b = pd.cut(df["wing_sum_asks"], bins=EDGES, labels=LBL, right=False)
    print("=" * 84)
    print(title)
    print("=" * 84)
    print(f"{'sum_asks band':<14}{'n':>5}{'%':>6}{'avgSum':>8}{'favAsk':>8}{'fav%':>7}{'wing%':>7}{'pm1%':>7}")
    for lbl in LBL:
        g = df[b == lbl]
        if g.empty:
            print(f"{lbl:<14}{0:>5}")
            continue
        print(f"{lbl:<14}{len(g):>5}{len(g) / n_tot:>6.0%}{g['wing_sum_asks'].mean():>8.2f}"
              f"{g['modal_ask'].mean():>8.2f}{g['top1'].mean():>7.0%}"
              f"{g['wing_covers'].mean():>7.0%}{g['top3'].mean():>7.0%}")


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    hi = df[df["kind"] == "high"]
    report(hi, "ALL high-temp cities, ALL anchor hours (pooled)")
    print()
    report(hi[hi["hour"] == 13], "ALL high-temp cities, 1 PM anchor only")
    print()
    report(hi[hi["city"] == "Chicago"], "Chicago high, all hours")


if __name__ == "__main__":
    main()
