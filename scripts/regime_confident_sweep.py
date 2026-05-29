"""Sweep regime_confident parameters to find a configuration that fires.

Default peak_p_floor=0.50 never fires (mean modal_p in Kalshi window = 0.275).
Lower the floor and widen the HRRR-gap to find any usable point.
"""

from __future__ import annotations
import subprocess, sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
BACKTEST = "scripts/backtest_strategy.py"


def run(tag, peak_p, hrrr_gap):
    out = f"data/regime_{tag}.parquet"
    cmd = [PY, BACKTEST, "--strategy", "regime_confident",
           "--regime-peak-p-floor", str(peak_p),
           "--regime-max-hrrr-gap-f", str(hrrr_gap),
           "--out", out]
    subprocess.run(cmd, capture_output=True, cwd=_ROOT)
    daily = pd.read_parquet(_ROOT / out)
    pos = pd.read_parquet(_ROOT / out.replace(".parquet", "_positions.parquet"))
    return daily, pos


def summarize(pos):
    if pos.empty:
        return dict(days=0, W=0, L=0, pnl=0, sharpe=0, max_dd=0, worst=0, best=0)
    day = pos.groupby("date").agg(net=("net_cents", "sum")).reset_index().sort_values("date")
    W = int((day["net"] > 0).sum())
    L = int((day["net"] < 0).sum())
    pnl = day["net"].sum() / 100
    best = day["net"].max() / 100
    worst = day["net"].min() / 100
    span = (day["date"].max() - day["date"].min()).days + 1
    s = pd.Series([0.0] * span)
    for _, r in day.iterrows():
        s.iloc[(r["date"] - day["date"].min()).days] = r["net"]
    sharpe = s.mean() / s.std() * np.sqrt(252) if s.std() > 0 else 0
    equity = s.cumsum() / 100
    dd = float((equity - equity.cummax()).min())
    return dict(days=len(day), W=W, L=L, pnl=pnl, sharpe=sharpe, max_dd=dd, worst=worst, best=best)


# Grid: peak_p_floor x hrrr_gap
peak_p_grid = [0.20, 0.25, 0.30, 0.40, 0.50]
hrrr_gap_grid = [4.0, 6.0, 8.0, 99.0]

print(f"{'peak_p':>6} {'hrrr_gap':>8} {'days':>5} {'W/L':>8} {'PnL$':>9} "
      f"{'best$':>8} {'worst$':>9} {'Sharpe':>7} {'MaxDD$':>9}")
print("-" * 80)
rows = []
for pp in peak_p_grid:
    for hg in hrrr_gap_grid:
        tag = f"p{int(pp*100):03d}_h{int(hg):03d}"
        _, pos = run(tag, pp, hg)
        m = summarize(pos)
        wl = f"{m['W']}/{m['L']}"
        print(f"{pp:>6.2f} {hg:>8.1f} {m['days']:>5} {wl:>8} {m['pnl']:>+9.2f} "
              f"{m['best']:>+8.2f} {m['worst']:>+9.2f} {m['sharpe']:>+7.2f} {m['max_dd']:>+9.2f}")
        rows.append({"peak_p": pp, "hrrr_gap": hg, **m})

# Best by PnL
profitable = [r for r in rows if r["pnl"] > 0 and r["days"] > 0]
print()
if profitable:
    profitable.sort(key=lambda r: -r["pnl"])
    print(f"Best profitable config: peak_p={profitable[0]['peak_p']}, "
          f"hrrr_gap={profitable[0]['hrrr_gap']}: PnL=${profitable[0]['pnl']:+.2f}, "
          f"Sharpe={profitable[0]['sharpe']:+.2f}")
else:
    print("No profitable configuration found across the grid.")
