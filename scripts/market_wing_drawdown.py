"""Drawdown anatomy for market_wing + drop_lower_ask (1 PM, v3).

Answers: how the sizing parameters translate into per-day stake, and WHEN the
drawdowns actually happen. Reads the daily + positions parquets produced by
  python scripts/backtest_strategy.py --strategy market_wing --wing-drop-lower-ask \
      --wing-assumed-win-prob 0.99 --wing-max-ask-sum 1.00 --out data/cmp_v3_1pm_mw.parquet

Run:  python scripts/market_wing_drawdown.py
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

STEM = sys.argv[1] if len(sys.argv) > 1 else "cmp_v3_1pm_mw"
ASSUMED_WP = 0.99   # --wing-assumed-win-prob used in the run


def kelly_full(p, s):
    """Binary Kelly fraction for win-prob p at total cost s (sum of asks)."""
    return p - (1 - p) * s / (1 - s) if s < 1 else np.nan


def main():
    daily = pd.read_parquet(_ROOT / f"data/{STEM}.parquet").sort_values("date").reset_index(drop=True)
    pos = pd.read_parquet(_ROOT / f"data/{STEM}_positions.parquet")

    # per-day sum_asks (Σ entry price across legs) and leg count
    legs = pos.groupby("date").agg(
        sum_asks=("entry_cents", lambda c: c.sum() / 100),
        n_legs=("ticker", "size"),
        max_leg_stake=("stake_cents", "max"),
    ).reset_index()
    d = daily.merge(legs, on="date", how="left")
    fired = d[d["n_targets"] > 0].copy()
    fired["bankroll_pre"] = fired["bankroll_usd"] - fired["day_pnl_cents"] / 100
    fired["stake_frac"] = (fired["day_stake_cents"] / 100) / fired["bankroll_pre"]
    fired["pnl_usd"] = fired["day_pnl_cents"] / 100
    fired["pnl_frac"] = fired["pnl_usd"] / fired["bankroll_pre"]
    fired["won_day"] = fired["day_pnl_cents"] > 0
    fired["ev_margin_assumed"] = ASSUMED_WP - fired["sum_asks"]

    n = len(fired)
    wr = fired["won_day"].mean()
    print("=" * 76)
    print(f"market_wing + drop_lower_ask — {STEM}")
    print(f"  fires={n}  win-rate={wr:.0%}  net PnL=${fired['pnl_usd'].sum():+.0f}")
    print("=" * 76)

    # ---- equity curve + max drawdown ----
    eq = daily.set_index("date")["bankroll_usd"]
    peak = eq.cummax()
    dd = eq - peak
    dd_pct = dd / peak
    trough_date = dd_pct.idxmin()
    peak_date = eq.loc[:trough_date].idxmax()
    rec = eq.loc[trough_date:]
    recov = rec[rec >= peak.loc[trough_date]]
    recov_date = recov.index[0] if len(recov) else None
    print("\nEQUITY / DRAWDOWN")
    print(f"  start ${eq.iloc[0]:.0f} → end ${eq.iloc[-1]:.0f}   peak ${peak.max():.0f}")
    print(f"  MAX DRAWDOWN {dd.min():+.0f}$ ({dd_pct.min():+.1%})")
    print(f"    peak  {peak_date.date()} (${eq.loc[peak_date]:.0f})")
    print(f"    trough{trough_date.date()} (${eq.loc[trough_date]:.0f})")
    print(f"    recovered {recov_date.date() if recov_date is not None else 'NOT within window'}")

    # days inside the peak→trough slide
    slide = fired[(fired["date"] > peak_date) & (fired["date"] <= trough_date)]
    print(f"  the slide spans {len(slide)} fired days; "
          f"{(~slide['won_day']).sum()} losses totaling ${slide.loc[~slide['won_day'],'pnl_usd'].sum():+.0f}")

    # ---- sizing diagnostics ----
    print("\nSIZING (how the params set stake)")
    print(f"  quarter-Kelly × throttle, assumed_win_prob={ASSUMED_WP}")
    print(f"  per-day stake fraction of bankroll: mean {fired['stake_frac'].mean():.1%}, "
          f"max {fired['stake_frac'].max():.1%}")
    for t, g in fired.groupby("throttle"):
        print(f"    throttle={t}: {len(g):>2} days, mean stake {g['stake_frac'].mean():.1%} of bankroll")
    cap = (fired["max_leg_stake"] >= 0.20 * fired["bankroll_pre"] * 100 - 1).mean()
    print(f"  per-contract cap (0.20) binds on ~{cap:.0%} of fires")

    # the assumed-vs-true win-prob over-bet
    s_med = fired["sum_asks"].median()
    print(f"\n  THE OVER-BET: median sum_asks={s_med:.2f}")
    print(f"    Kelly at assumed p=0.99 : {kelly_full(0.99, s_med):+.2f}  (what it bets)")
    print(f"    Kelly at realized p={wr:.2f} : {kelly_full(wr, s_med):+.2f}  (what edge actually justifies)")
    neg = (fired['sum_asks'] >= wr).mean()
    print(f"    fires with sum_asks ≥ realized WR ({wr:.0%}) = STRUCTURALLY -EV: {neg:.0%}")

    # ---- when losses occur ----
    losses = fired[~fired["won_day"]].sort_values("pnl_usd")
    print(f"\nLOSS DAYS ({len(losses)} of {n})  — sorted by size")
    print(f"  {'date':<12}{'thr':>4}{'sum_asks':>9}{'stake$':>8}{'loss$':>8}{'loss%bk':>8}  {'truth vs wing':<22}")
    for _, r in losses.iterrows():
        dp = pos[(pos["date"] == r["date"])]
        wing = "+".join(dp["bucket_spec"].tolist())
        print(f"  {str(r['date'].date()):<12}{r['throttle']:>4}{r['sum_asks']:>9.2f}"
              f"{r['day_stake_cents']/100:>8.0f}{r['pnl_usd']:>8.0f}{r['pnl_frac']:>8.1%}  "
              f"truth={r['actual_f']:>3}°F  wing={wing}")

    # loss vs win conditions
    w = fired[fired["won_day"]]
    print("\nLOSS vs WIN conditions (mean)")
    print(f"  {'':<12}{'sum_asks':>9}{'stake%bk':>9}{'peak_P':>8}")
    print(f"  {'losses':<12}{losses['sum_asks'].mean():>9.2f}{losses['stake_frac'].mean():>9.1%}{losses['peak_P'].mean():>8.1%}")
    print(f"  {'wins':<12}{w['sum_asks'].mean():>9.2f}{w['stake_frac'].mean():>9.1%}{w['peak_P'].mean():>8.1%}")


if __name__ == "__main__":
    main()
