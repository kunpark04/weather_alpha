"""Run variance, hrrr_bias, regime_confident; combine with previous retest results."""

from __future__ import annotations
import subprocess
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
BACKTEST = "scripts/backtest_strategy.py"


def run(tag, args):
    out = f"data/retest_{tag}.parquet"
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
    n = len(day)
    W = int((day["net"] > 0).sum())
    L = int((day["net"] < 0).sum())
    pnl = day["net"].sum() / 100
    fees = day["fees"].sum() / 100
    avg_W = day[day["net"] > 0]["net"].mean() / 100 if W else 0
    avg_L = day[day["net"] < 0]["net"].mean() / 100 if L else 0
    best = day["net"].max() / 100
    worst = day["net"].min() / 100
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


# New strategies
new_variants = [
    ("variance", "variance", ["--strategy", "variance"]),
    ("hrrr_bias", "hrrr_bias", ["--strategy", "hrrr_bias"]),
    ("regime_conf", "regime_confident", ["--strategy", "regime_confident"]),
]

print(f"Running {len(new_variants)} untested strategies...")
new_rows = []
for tag, name, args in new_variants:
    print(f"  ... {name}")
    daily, pos = run(tag, args)
    new_rows.append(metrics(name, daily, pos))

# Reload previously-tested variants from cached parquets
prev_variants = [
    ("joint_kelly",         "joint_kelly"),
    ("wing_strict",         "wing eq-payout (strict 0.97)"),
    ("wing_loose",          "wing eq-payout (loose 1.00)"),
    ("wing_dropworst",      "wing eq-payout + drop_worst_leg"),
    ("wing_droplow",        "wing eq-payout + drop_lower_ask"),
    ("wing_drophigh",       "wing eq-payout + drop_higher_ask (PLACEBO)"),
    ("wing_prob",           "wing prob-weighted"),
    ("wing_mkt3",           "wing market-weighted power=3"),
    ("wing_mkt5",           "wing market-weighted power=5"),
    ("wingany",             "wing_any (no agreement)"),
    ("wingany_droplow",     "wing_any + drop_lower_ask"),
    ("wingany_drophigh",    "wing_any + drop_higher_ask (PLACEBO)"),
]

prev_rows = []
for tag, name in prev_variants:
    out = _ROOT / f"data/retest_{tag}.parquet"
    if not out.exists():
        continue
    daily = pd.read_parquet(out)
    pos = pd.read_parquet(out.with_name(out.stem + "_positions.parquet"))
    prev_rows.append(metrics(name, daily, pos))

all_rows = prev_rows + new_rows

# Filter: profitable only
prof_rows = [r for r in all_rows if r["pnl"] > 0]
prof_rows.sort(key=lambda r: -r["pnl"])

print()
print("=" * 130)
print(f"PROFITABLE STRATEGIES ONLY ({len(prof_rows)} of {len(all_rows)})  --  sorted by PnL")
print("=" * 130)
print(f"{'Strategy':<42} {'days':>5} {'W/L':>8} {'WR%':>5} "
      f"{'PnL$':>9} {'fees$':>7} {'avg W$':>8} {'avg L$':>9} "
      f"{'best$':>8} {'worst$':>9} {'Sharpe':>7} {'MaxDD$':>9}")
print("-" * 130)
for r in prof_rows:
    wr = r["W"] / max(r["W"] + r["L"], 1) * 100
    print(f"{r['name']:<42} {r['days']:>5} {r['W']}/{r['L']:<6} "
          f"{wr:>4.0f}% {r['pnl']:>+9.2f} {r['fees']:>+7.2f} "
          f"{r['avg_W']:>+8.2f} {r['avg_L']:>+9.2f} "
          f"{r['best']:>+8.2f} {r['worst']:>+9.2f} "
          f"{r['sharpe']:>+7.2f} {r['max_dd']:>+9.2f}")

print()
print("Newly-tested strategies (full results, regardless of profitability):")
for r in new_rows:
    wr = r["W"] / max(r["W"] + r["L"], 1) * 100
    print(f"  {r['name']:<22} days={r['days']:>3} W/L={r['W']}/{r['L']:<3} WR={wr:>3.0f}% "
          f"PnL=${r['pnl']:+8.2f} Sharpe={r['sharpe']:+.2f} MaxDD=${r['max_dd']:+.2f}")
