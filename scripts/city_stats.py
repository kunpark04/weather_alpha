"""Per-anchor stats for one or more HIGH-temp cities, fee-aware.
    python scripts/city_stats.py "Houston" "San Francisco"
Columns: N settled days; Top-1/2/3 accuracy (all days); aff% + nAff (wing < $0.90);
avg wing cost; win% (coverage on affordable days = fire win rate); fee-aware net/fire (c)
and total net ($) for the $2.50 equal-payout drop_lower_ask wing.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from best_anchor import day_net, AFFORD  # noqa: E402

HOURS = [8, 9, 10, 11, 12, 13]


def main():
    cities = sys.argv[1:] or ["Houston", "San Francisco"]
    df = pd.read_parquet("data/accuracy_all.parquet")
    df = df[df["kind"] == "high"]

    def p(x):
        return "  -- " if x != x else f"{x:4.0%}"

    for city in cities:
        g = df[df["city"] == city]
        if g.empty:
            print(f"{city}: no data\n")
            continue
        print("=" * 90)
        print(f"{city.upper()} HIGH   tz={g['tz'].iloc[0]}   (fee-aware $2.50 equal-payout drop_lower_ask wing)")
        print("=" * 90)
        print(f"{'hour':>5}{'N':>4}{'T1':>6}{'T2':>6}{'T3':>6}{'favAsk':>8}{'aff%':>6}{'nAff':>5}"
              f"{'cost':>6}{'win%':>6}{'net/f¢':>8}{'totNet$':>9}")
        for h in HOURS:
            gh = g[g["hour"] == h]
            if gh.empty:
                continue
            aff = gh[gh["wing_sum_asks"] < AFFORD]
            nets = [day_net(s, m, c)[0] for s, m, c in
                    zip(aff["wing_sum_asks"], aff["modal_ask"], aff["wing_covers"])]
            n_aff = len(nets)
            cost = aff["wing_sum_asks"].mean() if n_aff else float("nan")
            winr = aff["wing_covers"].mean() if n_aff else float("nan")
            netf = (sum(nets) / n_aff * 100) if n_aff else float("nan")
            cstr = "  -- " if cost != cost else f"{cost:4.2f}"
            nfstr = "  -- " if netf != netf else f"{netf:+5.1f}"
            print(f"{h:>5}{len(gh):>4}{p(gh['top1'].mean()):>6}{p(gh['wing_covers'].mean()):>6}"
                  f"{p(gh['top3'].mean()):>6}{gh['modal_ask'].mean():>8.2f}{p(n_aff / len(gh)):>6}"
                  f"{n_aff:>5}{cstr:>6}{p(winr):>6}{nfstr:>8}{sum(nets):>+9.2f}")
        print()


if __name__ == "__main__":
    main()
