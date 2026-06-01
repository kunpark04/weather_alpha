"""Best intraday anchor per HIGH-temp city, FEE-AWARE.

Reconstructs the realized net P&L of the production wing (market_wing + drop_lower_ask,
equal-payout, flat $2.50) at each anchor hour from data/accuracy_all.parquet, using each
day's actual win/loss outcome, and picks the anchor that maximizes total net P&L over the
window. Fees are the real Kalshi schedule via weather_alpha.fees.trade_fee_cents
(ceil(7% * N * P * (1-P)) per leg, applied to BOTH legs at entry).

Per affordable day (2-leg wing, legs = favorite + better adjacent):
    K       = round($2.50 / sum_asks)          contracts per leg (equal payout)
    cost    = K * sum_asks                       premium paid (both legs)
    fee     = fee(a_modal, K) + fee(a_adj, K)    entry fee, both legs
    payout  = K  if winner inside the wing else 0
    net     = payout - cost - fee

Caveats: reconstructed from candlestick yes_ask at the hour (fill = ask); integer-K
rounding approximates the engine; no full strategy gates / placebo. A screen, not a verdict.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from weather_alpha.fees import trade_fee_cents  # noqa: E402

STAKE, AFFORD, MIN_FIRES = 2.50, 0.90, 10
HOURS = [8, 9, 10, 11, 12, 13]


def day_net(sum_asks: float, modal_ask: float, covered: bool):
    a_m = float(modal_ask)
    a_j = max(0.0, float(sum_asks) - a_m)
    K = max(1, round(STAKE / float(sum_asks)))
    fee = (trade_fee_cents(a_m, K) + trade_fee_cents(a_j, K)) / 100.0
    payout = float(K) if covered else 0.0
    return payout - K * float(sum_asks) - fee, fee


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    df = df[df["kind"] == "high"].copy()
    N_by = df.groupby(["city", "hour"]).size().rename("N").reset_index()

    aff = df[df["wing_sum_asks"] < AFFORD].copy()
    res = [day_net(s, m, c) for s, m, c in zip(aff["wing_sum_asks"], aff["modal_ask"], aff["wing_covers"])]
    aff["net"] = [r[0] for r in res]
    aff["fee"] = [r[1] for r in res]

    cell = aff.groupby(["city", "hour"]).agg(
        n_fires=("net", "size"), win=("wing_covers", "mean"), cost=("wing_sum_asks", "mean"),
        fee=("fee", "mean"), net_fire=("net", "mean"), total=("net", "sum"),
    ).reset_index().merge(N_by, on=["city", "hour"], how="left")
    cell["aff"] = cell["n_fires"] / cell["N"]

    best = []
    for city, gc in cell.groupby("city"):
        elig = gc[gc["n_fires"] >= MIN_FIRES]
        src = elig if not elig.empty else gc
        best.append(src.loc[src["total"].idxmax()])
    best = pd.DataFrame(best)
    bh = dict(zip(best["city"], best["hour"]))

    print("=" * 80)
    print("FEE-AWARE NET P&L PER FIRE (cents)  -  HIGH temp, $2.50 equal-payout drop_lower_ask wing")
    print("(* = city's best anchor by total net $ over the window;  -- = no affordable days)")
    print("=" * 80)
    print(f"{'city':<16}" + "".join(f"{str(h) + ':00':>8}" for h in HOURS))
    for city in sorted(cell["city"].unique()):
        row = f"{city:<16}"
        for h in HOURS:
            r = cell[(cell.city == city) & (cell.hour == h)]
            if r.empty:
                row += f"{'--':>8}"
            else:
                mark = "*" if bh.get(city) == h else " "
                row += f"{r['net_fire'].iloc[0] * 100:>+7.1f}{mark}"
        print(row)

    print("\n" + "=" * 80)
    print("BEST ANCHOR PER HIGH-TEMP CITY (fee-aware, ranked by total net $)")
    print("=" * 80)
    print(f"{'city':<16}{'anchor':>7}{'nFire':>6}{'aff%':>6}{'win%':>6}{'cost':>6}"
          f"{'fee/f':>7}{'net/f':>7}{'total$':>8}  TEST?")
    for _, r in best.sort_values("total", ascending=False).iterrows():
        test = "YES" if (r["net_fire"] > 0.01 and r["n_fires"] >= 15 and r["total"] > 0) else "no"
        if r["city"] == "Chicago":
            test += " (prod)"
        print(f"{r['city']:<16}{int(r['hour']):>5}:00{int(r['n_fires']):>6}{r['aff']:>6.0%}{r['win']:>6.0%}"
              f"{r['cost']:>6.2f}{r['fee'] * 100:>6.1f}{r['net_fire'] * 100:>+7.1f}{r['total']:>+8.2f}  {test}")
    cell.to_csv("data/best_anchor_high.csv", index=False)
    print("\nnet/f, fee/f in cents; total$ = realized net over the ~67-day window at $2.50/fire.")
    print("TEST? = net/fire > 1c AND nFire >= 15 AND total$ > 0.   full grid -> data/best_anchor_high.csv")


if __name__ == "__main__":
    main()
