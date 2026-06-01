"""Strategy test: on sum_asks > 1 days, BUY ONLY the single Top-1 (favorite) bucket.

Single-bucket buy at the favorite's ask. $2.50 flat, integer contracts, real Kalshi fees
(one leg => roughly half the wing's fee). Realized per day:
    N      = round($2.50 / modal_ask)
    cost   = N * modal_ask
    fee    = ceil(7% * N * p * (1-p))            (one leg)
    payout = N  if the favorite bucket won (top1) else 0
    net    = payout - cost - fee
Profitable iff the favorite wins MORE than its ask implies: gap = Top1_rate - modal_ask > fee drag.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from weather_alpha.fees import trade_fee_cents  # noqa: E402

STAKE = 2.50


def day_net(p, won):
    N = max(1, round(STAKE / p))
    fee = trade_fee_cents(p, N) / 100.0
    return (N if won else 0.0) - N * p - fee, fee


def summarize(g):
    res = [day_net(p, w) for p, w in zip(g["modal_ask"], g["top1"])]
    nets = [r[0] for r in res]
    fees = [r[1] for r in res]
    n = len(nets)
    return {"n": n, "top1": g["top1"].mean() if n else float("nan"),
            "price": g["modal_ask"].mean() if n else float("nan"),
            "gap": (g["top1"].mean() - g["modal_ask"].mean()) if n else float("nan"),
            "fee": (sum(fees) / n) if n else float("nan"),
            "netf": (sum(nets) / n) if n else float("nan"), "total": sum(nets)}


def prow(label, s):
    if s["n"] == 0:
        print(f"{label:<16}{0:>5}")
        return
    print(f"{label:<16}{s['n']:>5}{s['top1']:>7.0%}{s['price']:>8.2f}{s['gap'] * 100:>+8.1f}"
          f"{s['fee'] * 100:>7.1f}{s['netf'] * 100:>+8.1f}{s['total']:>+9.2f}")


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    s = df[(df["kind"] == "high") & (df["wing_sum_asks"] > 1.0)]
    hdr = f"{'segment':<16}{'n':>5}{'Top1%':>7}{'price':>8}{'gapPts':>8}{'fee/f':>7}{'net/f¢':>8}{'total$':>9}"

    print("=" * 70)
    print("BUY SINGLE TOP-1 (favorite) on sum>1 days — fee-aware, $2.50/trade")
    print("=" * 70)
    print(hdr)
    prow("POOLED all-hours", summarize(s))
    prow("1 PM only", summarize(s[s["hour"] == 13]))
    print("\nPer city (1 PM, sum>1):")
    print(hdr)
    for city, g in sorted(s[s["hour"] == 13].groupby("city"), key=lambda kv: -summarize(kv[1])["total"]):
        prow(city, summarize(g))
    print("\nChicago by sub-band (all hours, sum>1):")
    print(hdr)
    cg = s[s["city"] == "Chicago"]
    for lo, hi, lbl in [(1.00, 1.05, "1.00-1.05"), (1.05, 1.10, "1.05-1.10"), (1.10, 9.0, ">=1.10")]:
        prow(lbl, summarize(cg[(cg["wing_sum_asks"] >= lo) & (cg["wing_sum_asks"] < hi)]))
    print("\ngap = Top1_rate - favorite ask. Need gap > ~+2 pts (single-leg fee) to profit.")


if __name__ == "__main__":
    main()
