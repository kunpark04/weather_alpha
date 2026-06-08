"""Top-2 coverage break-even, UNCONDITIONAL vs GATED (max_ask_sum <= 0.90).

For an equal-payout coverage of the two highest-priced buckets you pay (a1+a2) to receive
$1 if either wins, so per city-day:  profit = win - (a1+a2) - fees ; break-even = cost + fees.

This reports the top-2 economics at the 1 PM anchor two ways:
  ALL    every day
  GATED  only days where cost (a1+a2) <= 0.90  -- the production max_ask_sum condition;
         also reports the FIRE RATE (how often the gate triggers) since the gate's whole
         value is selecting the cheap-coverage subset. The risk it tests: do cheap days
         have a LOWER win rate (adverse selection) that cancels the cheaper cost?

Cost uses the last-trade + 1c ask proxy (backfill has trades, not the live book), so the true
cost/break-even is likely a touch higher -> edges are an optimistic bound. Fees = 7%*p*(1-p)/leg.
"""
from __future__ import annotations
import statistics as st
import sys
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.modal_rank_multicity import CITY, load_city, BACKFILL

GATE = 0.90


def fee_dollars(p: float) -> float:
    return 0.07 * p * (1 - p)


def per_day(series: str, hour: int = 13):
    """List of (cost, win:bool, fee) per city-day at the anchor."""
    df = load_city(series)
    if df.empty:
        return []
    tz = ZoneInfo(CITY[series][1])
    out = []
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        win_tk = w[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        anchor = pd.Timestamp(ev.year, ev.month, ev.day, hour, tz=tz).tz_convert("UTC")
        last = day[day["timestamp"] <= anchor].groupby("ticker")["yes_price_cents"].last()
        if last.size < 3:
            continue
        top2 = list(last.sort_values(ascending=False).index[:2])
        asks = [min((last[t] + 1) / 100.0, 0.99) for t in top2]
        out.append((sum(asks), win_tk in set(top2), sum(fee_dollars(a) for a in asks)))
    return out


def agg(days):
    if not days:
        return None
    cost = st.mean(d[0] for d in days)
    wr = st.mean(1.0 if d[1] else 0.0 for d in days)
    fee = st.mean(d[2] for d in days)
    return len(days), wr, cost, fee, cost + fee, wr - (cost + fee)


def main(argv):
    hour = int(argv[0]) if argv else 13
    data = {CITY[s][0]: per_day(s, hour) for s in CITY if (BACKFILL / s).is_dir()}

    print(f"GATED top-2 break-even @ {hour}:00 local  (only days cost<= {GATE:.2f}; the max_ask_sum gate)")
    print(f"{'city':<15}{'fire':>10}{'winrate':>9}{'cost':>7}{'breakeven':>11}{'edge':>8}")
    print("-" * 60)
    pool_all, pool_gate = [], []
    rows = []
    for city, days in data.items():
        gated = [d for d in days if d[0] <= GATE]
        pool_all += days; pool_gate += gated
        a = agg(gated)
        rows.append((city, len(days), gated, a))
    for city, n_all, gated, a in sorted(rows, key=lambda x: -(x[3][5] if x[3] else -9)):
        if not a:
            print(f"{city:<15}{'0/' + str(n_all):>10}{'-- no gated days --':>40}")
            continue
        n, wr, cost, fee, be, edge = a
        fire = f"{n}/{n_all} ({n/n_all:.0%})"
        flag = "" if n >= 20 else "  (N<20!)"
        print(f"{city:<15}{fire:>10}{wr:>8.0%}{cost:>7.0%}{be:>10.0%}{edge:>+7.1%}{flag}")
    print("-" * 60)
    for label, pool in (("POOLED-ALL", pool_all), (f"POOLED-GATED", pool_gate)):
        a = agg(pool)
        if a:
            n, wr, cost, fee, be, edge = a
            fire = f"{n}/{len(pool_all)} ({n/len(pool_all):.0%})" if "GATED" in label else f"{n}"
            print(f"{label:<15}{fire:>10}{wr:>8.0%}{cost:>7.0%}{be:>10.0%}{edge:>+7.1%}")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
