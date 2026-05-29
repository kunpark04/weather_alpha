"""Test the market_wing variant: anchor the wing around market modal instead of model.

Reasoning: §10.3 showed the market is 64% top-1 / 98% top-3 vs model's 44% / 92%.
On disagreement days the market is 59% top-1 vs model's 15%.  Anchoring on market
should fire on all 61 Kalshi days and benefit from the market's superior accuracy.

Tests:
  1. market_wing baseline (3-leg, equal-payout)
  2. market_wing + drop_lower_ask
  3. market_wing + drop_higher_ask (PLACEBO)
"""

from __future__ import annotations
import subprocess, sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
BACKTEST = "scripts/backtest_strategy.py"


def run(tag, args):
    out = f"data/mw_{tag}.parquet"
    cmd = [PY, BACKTEST] + args + ["--out", out]
    subprocess.run(cmd, capture_output=True, cwd=_ROOT)
    daily = pd.read_parquet(_ROOT / out)
    pos = pd.read_parquet(_ROOT / out.replace(".parquet", "_positions.parquet"))
    return daily, pos


def metrics(name, daily, pos):
    if pos.empty:
        return dict(name=name, days=0, W=0, L=0, pnl=0, fees=0,
                    avg_W=0, avg_L=0, best=0, worst=0, sharpe=0, max_dd=0)
    day = pos.groupby("date").agg(
        net=("net_cents", "sum"), fees=("fee_cents", "sum"),
    ).reset_index().sort_values("date")
    n = len(day); W = int((day["net"] > 0).sum()); L = int((day["net"] < 0).sum())
    pnl = day["net"].sum() / 100; fees = day["fees"].sum() / 100
    avg_W = day[day["net"] > 0]["net"].mean() / 100 if W else 0
    avg_L = day[day["net"] < 0]["net"].mean() / 100 if L else 0
    best = day["net"].max() / 100; worst = day["net"].min() / 100
    span = (day["date"].max() - day["date"].min()).days + 1
    s = pd.Series([0.0] * span)
    for _, r in day.iterrows():
        s.iloc[(r["date"] - day["date"].min()).days] = r["net"]
    sharpe = s.mean() / s.std() * np.sqrt(252) if s.std() > 0 else 0
    equity = s.cumsum() / 100
    dd = float((equity - equity.cummax()).min())
    return dict(name=name, days=n, W=W, L=L, pnl=pnl, fees=fees,
                avg_W=avg_W, avg_L=avg_L, best=best, worst=worst,
                sharpe=sharpe, max_dd=dd)


COMMON = ["--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "1.00"]

variants = [
    ("baseline", "market_wing baseline (3-leg eq-payout)",
     ["--strategy", "market_wing"] + COMMON),

    ("droplow", "market_wing + drop_lower_ask (2-leg)",
     ["--strategy", "market_wing", "--wing-drop-lower-ask"] + COMMON),

    ("drophigh", "market_wing + drop_higher_ask (PLACEBO)",
     ["--strategy", "market_wing", "--wing-drop-higher-ask"] + COMMON),
]

print(f"Running {len(variants)} market_wing variants...")
rows = []
for tag, name, args in variants:
    print(f"  ... {name}")
    daily, pos = run(tag, args)
    rows.append(metrics(name, daily, pos))

print()
print("=" * 130)
print(f"{'Strategy':<48} {'days':>5} {'W/L':>8} {'WR%':>5} "
      f"{'PnL$':>9} {'fees$':>7} {'avg W$':>8} {'avg L$':>9} "
      f"{'best$':>8} {'worst$':>9} {'Sharpe':>7} {'MaxDD$':>9}")
print("-" * 130)
for r in rows:
    wr = r["W"] / max(r["W"] + r["L"], 1) * 100
    print(f"{r['name']:<48} {r['days']:>5} {r['W']}/{r['L']:<6} "
          f"{wr:>4.0f}% {r['pnl']:>+9.2f} {r['fees']:>+7.2f} "
          f"{r['avg_W']:>+8.2f} {r['avg_L']:>+9.2f} "
          f"{r['best']:>+8.2f} {r['worst']:>+9.2f} "
          f"{r['sharpe']:>+7.2f} {r['max_dd']:>+9.2f}")

# Identify which Kalshi days did NOT fire on market_wing baseline (expected: market modal at tail)
k = pd.read_parquet(_ROOT / "data/kalshi_history.parquet")
k["event_date"] = pd.to_datetime(k["event_date"]).dt.normalize()
all_kalshi = set(k["event_date"].unique())
fired = set(pd.read_parquet(_ROOT / "data/mw_baseline_positions.parquet")["date"].dt.normalize().unique())
missed = sorted(all_kalshi - fired)
print()
print(f"Kalshi days that didn't fire on market_wing baseline (out of {len(all_kalshi)}): {len(missed)}")
for d in missed[:20]:
    print(f"  {pd.Timestamp(d).date()}")
if len(missed) > 20:
    print(f"  ... ({len(missed) - 20} more)")
