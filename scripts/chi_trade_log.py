"""Chicago @ 1 PM trade-by-trade log (flat $2.50, fee-aware), with IS/OOS split marker.
'Trades' = the affordable fires (wing sum_asks < 0.90). IS/OOS = chronological midpoint of
Chicago's days (matching scripts/oos_split.py)."""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from size_by_sum import net_for_stake  # noqa: E402

START, AFFORD, STAKE = 25.0, 0.90, 2.50


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    chi = df[(df["kind"] == "high") & (df["city"] == "Chicago")]
    dates = sorted(chi["date"].unique())
    is_dates = set(dates[:len(dates) // 2])

    fires = chi[(chi["hour"] == 13) & (chi["wing_sum_asks"] < AFFORD)].sort_values("date")
    bank = START
    is_pnl = oos_pnl = 0.0
    is_w = is_l = oos_w = oos_l = 0

    print(f"CHICAGO @ 1 PM trade log — flat ${STAKE}, fee-aware, start ${START:.0f}. "
          f"{len(fires)} fires (IS/OOS split at date midpoint).\n")
    print(f"{'#':>3}  {'date':<12}{'split':<5}{'cost':>6}{'K':>4}{'W/L':>5}{'net$':>8}{'bank$':>9}")
    for i, (_, r) in enumerate(fires.iterrows(), 1):
        covered = int(r["wing_covers"])
        K = max(1, round(STAKE / r["wing_sum_asks"]))
        net = net_for_stake(r["wing_sum_asks"], r["modal_ask"], covered, STAKE)
        bank += net
        split = "IS" if r["date"] in is_dates else "OOS"
        if split == "IS":
            is_pnl += net; is_w += covered; is_l += 1 - covered
        else:
            oos_pnl += net; oos_w += covered; oos_l += 1 - covered
        print(f"{i:>3}  {str(pd.Timestamp(r['date']).date()):<12}{split:<5}{r['wing_sum_asks']:>6.2f}"
              f"{K:>4}{'W' if covered else 'L':>5}{net:>+8.2f}{bank:>9.2f}")

    print(f"\nIN-SAMPLE : {is_w}W / {is_l}L   net {is_pnl:+.2f}$")
    print(f"OUT-SAMPLE: {oos_w}W / {oos_l}L   net {oos_pnl:+.2f}$")
    print(f"TOTAL     : {is_w + oos_w}W / {is_l + oos_l}L   net {is_pnl + oos_pnl:+.2f}$   "
          f"final bankroll ${bank:.2f}")


if __name__ == "__main__":
    main()
