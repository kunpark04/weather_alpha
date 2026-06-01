"""Top-1 / Top-2 / Top-3 settlement accuracy in the sum_asks > 1 regime (HIGH temp).

  Top-1 = favorite (modal, highest yes_ask) bucket was the settled winner.
  Top-2 = winner inside the 2-leg wing {modal, higher-ask adjacent} (= drop_lower_ask).
  Top-3 = winner within modal +/-1 (3 buckets).
Accuracy is realized vs the settled winning bucket. Sub-banded to expose any overconfidence
tail. Watch the n column — the highest bands are sparse.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BANDS = [(1.00, 1.05), (1.05, 1.10), (1.10, 1.20), (1.20, 9.0)]


def line(g, label):
    if g.empty:
        print(f"{label:<13}{0:>5}")
        return
    print(f"{label:<13}{len(g):>5}{g['modal_ask'].mean():>8.2f}"
          f"{g['top1'].mean():>8.0%}{g['wing_covers'].mean():>8.0%}{g['top3'].mean():>8.0%}")


def report(df, title):
    s = df[df["wing_sum_asks"] > 1.0]
    print("=" * 58)
    print(title)
    print("=" * 58)
    print(f"{'sum band':<13}{'n':>5}{'favAsk':>8}{'Top-1':>8}{'Top-2':>8}{'Top-3':>8}")
    line(s, "ALL >1.00")
    for lo, hi in BANDS:
        seg = s[(s["wing_sum_asks"] >= lo) & (s["wing_sum_asks"] < hi)]
        line(seg, f">={lo:.2f}" if hi >= 9 else f"{lo:.2f}-{hi:.2f}")
    print()


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    df = df[df["kind"] == "high"]
    report(df, "ALL high cities, ALL hours (rows = city-day-hour, NOT independent)")
    report(df[df["hour"] == 13], "ALL high cities, 1 PM only (1 row per city-day)")
    report(df[df["city"] == "Chicago"], "Chicago high, all hours")


if __name__ == "__main__":
    main()
