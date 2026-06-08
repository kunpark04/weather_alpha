"""Root-cause: what separates the profitable city-backtests from the losing ones?

Instruments the SAME production wing (market_wing, drop_lower_ask, max_ask_sum<=0.90) over each
city's backfill and records, per FIRED day (a day the wing actually entered):
  - n_legs              how many buckets the wing bought that day (~2 after drop_lower_ask)
  - covered             did one of those buckets settle 'yes' (winner inside the wing)
  - sum_asks            per-$1-payout entry cost = sum of the kept legs' yes_ask
  - day PnL (flat $2.50) realized

Per city it then reports the decomposition that governs profit:
      EV per fired day  ~  K * (coverage - mean_cost - fee)   ==  K * EDGE
  so EDGE = coverage - mean_cost - fee, and a city is profitable iff coverage > mean_cost+fee.

It also answers 'why is the per-LEG win rate < 50% over ~60 days': the wing holds ~2 mutually
exclusive buckets and at most ONE can settle yes, so per-leg WR = wins/legs ~= coverage / legs/day
(<= 50%); it is NOT a per-day success rate and is independent of the day count.

'Tail losses (more than expected)' = MISS days (winner outside the wing -> full stake lost). The
market prices the cover at breakeven = mean_cost+fee, i.e. an implied miss rate 1-breakeven; the
realized excess = breakeven - coverage (= -edge): >0 means MORE misses than the market priced.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import _dummy_pred, _snapshot
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config
from weather_alpha.fees import trade_fee_cents
from weather_alpha.strategy import run_wing_strategy

FEE1 = lambda a: 0.07 * a * (1 - a)        # per-$1-payout fee (edge_by_anchor convention)


def rca_city(df: pd.DataFrame, s: str, cfg) -> dict | None:
    tz = ZoneInfo(CITY[s][1])
    fired = legs = wins = covered = 0
    costs, fee1s, pnl_days, miss_pnl, cover_pnl = [], [], [], [], []
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
            require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
            flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
            drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.90)
        if not out.targets:
            continue
        fired += 1
        leg_asks, d_pnl, d_cov = [], 0, 0
        for tgt in out.targets:
            c = next((x for x in contracts if x.ticker == tgt.ticker), None)
            if c is None:
                continue
            a = c.yes_ask if tgt.side == "yes" else c.no_ask
            n = tgt.target_contracts
            won = (tgt.ticker == winner) if tgt.side == "yes" else (tgt.ticker != winner)
            d_pnl += (n * 100 if won else 0) - round(a * 100) * n - trade_fee_cents(a, n)
            legs += 1; wins += int(won); leg_asks.append(a)
            d_cov = d_cov or int(won)
        covered += d_cov
        costs.append(sum(leg_asks)); fee1s.append(sum(FEE1(a) for a in leg_asks))
        pnl_days.append(d_pnl / 100.0)
        (cover_pnl if d_cov else miss_pnl).append(d_pnl / 100.0)
    if fired == 0:
        return None
    coverage = covered / fired
    mean_cost = float(np.mean(costs)); mean_fee = float(np.mean(fee1s))
    pnl = float(np.sum(pnl_days)); dp = np.array(pnl_days)
    return {
        "city": CITY[s][0], "series": s, "fired": fired, "legs_per_day": round(legs / fired, 2),
        "wr_per_leg": round(wins / legs, 4), "coverage": round(coverage, 4),
        "mean_cost": round(mean_cost, 4), "mean_fee": round(mean_fee, 4),
        "breakeven": round(mean_cost + mean_fee, 4),
        "edge": round(coverage - mean_cost - mean_fee, 4),
        "miss_days": fired - covered, "miss_rate": round(1 - coverage, 4),
        "excess_miss": round((mean_cost + mean_fee) - coverage, 4),   # = -edge ; >0 = more misses than priced
        "mean_miss_loss": round(float(np.mean(miss_pnl)), 2) if miss_pnl else 0.0,
        "worst_day": round(float(dp.min()), 2), "flat_pnl": round(pnl, 2),
        "profitable": bool(pnl > 0),
    }


def main():
    cfg = load_config()
    rows = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        r = rca_city(df, s, cfg)
        if r:
            rows.append(r)
    rows.sort(key=lambda r: -r["flat_pnl"])

    print("\n" + "=" * 112)
    print("PER-CITY DECOMPOSITION  (1 PM wing, drop_lower_ask, gate<=0.90; cost/fee/edge are per-$1 payout)")
    print("=" * 112)
    h = ("city", "fire", "lg/d", "WRleg", "cover", "cost", "fee", "b/e", "edge", "miss", "xMiss", "wDay", "PnL$")
    print(f"{h[0]:<14}{h[1]:>5}{h[2]:>6}{h[3]:>7}{h[4]:>7}{h[5]:>7}{h[6]:>6}{h[7]:>7}"
          f"{h[8]:>8}{h[9]:>6}{h[10]:>7}{h[11]:>7}{h[12]:>9}")
    for r in rows:
        print(f"{r['city']:<14}{r['fired']:>5}{r['legs_per_day']:>6.2f}{r['wr_per_leg']:>7.0%}"
              f"{r['coverage']:>7.0%}{r['mean_cost']:>7.3f}{r['mean_fee']:>6.3f}{r['breakeven']:>7.3f}"
              f"{r['edge']:>+8.3f}{r['miss_days']:>6}{r['excess_miss']:>+7.3f}{r['worst_day']:>+7.2f}"
              f"{r['flat_pnl']:>+9.2f}")

    win = [r for r in rows if r["profitable"]]
    los = [r for r in rows if not r["profitable"]]

    def avg(g, k):
        return float(np.mean([r[k] for r in g]))

    print("\n" + "=" * 112)
    print(f"GROUP MEANS    PROFITABLE ({len(win)} cities)   vs   LOSING ({len(los)} cities)")
    print("=" * 112)
    for k, lab in [("coverage", "coverage (winner inside wing)"), ("mean_cost", "mean cost (sum of asks, per $1)"),
                   ("breakeven", "breakeven (cost+fee)"), ("edge", "edge = coverage - breakeven"),
                   ("miss_rate", "miss rate (1 - coverage)"), ("excess_miss", "excess miss (breakeven - coverage)"),
                   ("wr_per_leg", "per-leg win rate"), ("legs_per_day", "legs per fired day")]:
        print(f"  {lab:<36}  profitable {avg(win,k):>+8.3f}   losing {avg(los,k):>+8.3f}   "
              f"gap {avg(win,k)-avg(los,k):>+8.3f}")

    # WR-from-coverage identity check (the 'why <50%' answer)
    print("\nWR check (per-leg WR should ~= coverage / legs_per_day):")
    for r in rows[:1] + rows[-1:]:
        print(f"  {r['city']:<14} coverage {r['coverage']:.0%} / {r['legs_per_day']:.2f} legs "
              f"= {r['coverage']/r['legs_per_day']:.0%}   (actual WRleg {r['wr_per_leg']:.0%})")
    return rows


if __name__ == "__main__":
    main()
