"""Flat-$ vs %-of-bankroll (compounding) sizing on Chicago @ 1 PM, fee-aware.
Flat $2.50 == 10% of the $25 start, so the FIRST bet matches; the difference is that
%-of-bankroll compounds (bigger bets after wins, smaller after losses)."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from size_by_sum import net_for_stake  # noqa: E402

START, AFFORD = 25.0, 0.90


def simulate(days, f=None, flat=None):
    bank = peak = START
    maxdd = maxdd_pct = 0.0
    for _, r in days.iterrows():
        stake = flat if flat is not None else f * bank
        if stake <= 0.01:
            continue
        bank += net_for_stake(r["wing_sum_asks"], r["modal_ask"], int(r["wing_covers"]), stake)
        peak = max(peak, bank)
        maxdd = max(maxdd, peak - bank)
        maxdd_pct = max(maxdd_pct, (peak - bank) / peak)
    return bank, maxdd, maxdd_pct


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    g = df[(df["kind"] == "high") & (df["city"] == "Chicago") & (df["hour"] == 13)
           & (df["wing_sum_asks"] < AFFORD)].sort_values("date")
    print(f"Chicago @ 1 PM — {len(g)} affordable fires, start ${START:.0f}, fee-aware (IN-SAMPLE sequence)\n")
    print(f"{'sizing':<16}{'final $':>9}{'return':>9}{'maxDD $':>9}{'maxDD %':>9}")
    fb, fd, fp = simulate(g, flat=2.50)
    print(f"{'flat $2.50':<16}{fb:>9.2f}{fb / START - 1:>9.1%}{fd:>9.2f}{fp:>9.1%}")
    for f in (0.05, 0.10, 0.25):
        b, d, p = simulate(g, f=f)
        print(f"{f'{f:.0%} of bankroll':<16}{b:>9.2f}{b / START - 1:>9.1%}{d:>9.2f}{p:>9.1%}")
    print("\nNote: in-sample (same ~25 Chicago fires); shows the compounding dynamics, not a forward number.")


if __name__ == "__main__":
    main()
