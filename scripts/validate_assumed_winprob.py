"""Validate strategy.wing.assumed_win_prob (=0.92) against realized wing win rates.

For the LIVE config (market_wing, drop_lower_ask, max_ask_sum<=0.90, flat) this measures, on
every day the strategy FIRES, whether the settled bucket actually fell inside the (usually
2-leg) wing -- the empirical analogue of assumed_win_prob. It also reports the mean wing COST
(sum of leg asks) so we can check the real EV condition (win_rate > cost + fees), since on the
flat path assumed_win_prob does NOT gate or size (the 0.90 ask cap binds first, Kelly is bypassed)
-- it only matters if you raise min_ev_margin or switch to Kelly.

Truth = Kalshi settlement_value. Ask = last+1c proxy (optimistic). Spring-2026, ~66 days/city.
"""
from __future__ import annotations

import statistics as st
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import _dummy_pred, _snapshot
from scripts.modal_rank_multicity import CITY, load_city, BACKFILL
from weather_alpha.config import load_config
from weather_alpha.fees import trade_fee_cents
from weather_alpha.strategy import run_wing_strategy

ASSUMED = 0.92


def validate(series: str, cfg):
    df = load_city(series)
    if df.empty:
        return None
    tz = ZoneInfo(CITY[series][1])
    wins, costs, fees, legs = [], [], [], []
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        winner = w[0]
        anchor = pd.Timestamp(ev.year, ev.month, ev.day, 13, tz=tz).tz_convert("UTC")
        contracts = _snapshot(day, anchor)
        if len(contracts) < 2:
            continue
        out = run_wing_strategy(
            cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
            require_agreement=False, wing_anchor="market", assumed_win_prob=ASSUMED,
            flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
            drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.90,
        )
        if not out.targets:
            continue
        leg_asks = [next(c.yes_ask for c in contracts if c.ticker == t.ticker) for t in out.targets]
        wins.append(1.0 if any(t.ticker == winner for t in out.targets) else 0.0)
        costs.append(sum(leg_asks))
        fees.append(sum(trade_fee_cents(a, 1) for a in leg_asks) / 100.0)
        legs.append(len(out.targets))
    if not wins:
        return None
    wr = st.mean(wins)
    cost = st.mean(costs)
    fee = st.mean(fees)
    return {"fires": len(wins), "win_rate": wr, "mean_legs": st.mean(legs),
            "mean_cost": cost, "mean_fee": fee, "breakeven": cost + fee,
            "edge": wr - cost - fee, "vs_assumed": wr - ASSUMED}


def main():
    cfg = load_config()
    rows = {}
    pooled_w, pooled_c, pooled_f = [], [], []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        r = validate(s, cfg)
        if r:
            rows[CITY[s][0]] = r
    print(f"assumed_win_prob = {ASSUMED}.  Realized WING win rate (settled bucket in the wing | FIRE):")
    print(f"{'city':<15}{'fires':>6}{'legs':>6}{'winrate':>9}{'vs.92':>8}{'cost':>7}{'fee':>6}{'breakeven':>11}{'edge':>8}")
    print("-" * 76)
    for city, m in sorted(rows.items(), key=lambda kv: -kv[1]["win_rate"]):
        print(f"{city:<15}{m['fires']:>6}{m['mean_legs']:>6.1f}{m['win_rate']:>8.0%}{m['vs_assumed']:>+8.1%}"
              f"{m['mean_cost']:>7.0%}{m['mean_fee']*100:>5.1f}c{m['breakeven']:>10.0%}{m['edge']:>+7.1%}")
    # fires-weighted pooled from the per-city rows
    tw = sum(m["win_rate"] * m["fires"] for m in rows.values())
    tc = sum(m["mean_cost"] * m["fires"] for m in rows.values())
    tf = sum(m["mean_fee"] * m["fires"] for m in rows.values())
    nf = sum(m["fires"] for m in rows.values())
    print("-" * 76)
    print(f"{'POOLED':<15}{nf:>6}{'':>6}{tw/nf:>8.0%}{tw/nf-ASSUMED:>+8.1%}{tc/nf:>7.0%}{tf/nf*100:>5.1f}c"
          f"{(tc+tf)/nf:>10.0%}{(tw-tc-tf)/nf:>+7.1%}")
    print("\nNote: on the LIVE (flat) path assumed_win_prob neither gates nor sizes (the 0.90 ask cap")
    print("binds before the EV gate, and flat sizing bypasses Kelly). The EDGE column = realized")
    print("win_rate - cost - fees is the true profitability test; vs.92 checks the assumption itself.")


if __name__ == "__main__":
    main()
