"""OOS-style validation for drop_lower_ask strategy.

Three tests:
  1. drop_lower_ask on early half (first 50% of trade days chronologically)
  2. drop_lower_ask on late half
  3. drop_HIGHER_ask placebo on full sample (predict against market)

If (1) and (2) both positive AND (3) decisively negative → high-confidence signal.
"""

from __future__ import annotations
import subprocess
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
BACKTEST = "scripts/backtest_strategy.py"


def run(args, out_name):
    cmd = [PY, BACKTEST] + args + ["--out", f"data/{out_name}.parquet"]
    subprocess.run(cmd, capture_output=True, cwd=_ROOT)
    daily = pd.read_parquet(_ROOT / f"data/{out_name}.parquet")
    pos = pd.read_parquet(_ROOT / f"data/{out_name}_positions.parquet")
    return daily, pos


def summarize(name, pos):
    if pos.empty:
        print(f"{name}: NO TRADES")
        return None
    day_summary = pos.groupby("date").agg(
        net=("net_cents", "sum"),
        n_legs=("ticker", "count"),
    ).reset_index().sort_values("date")
    n_days = len(day_summary)
    wins = (day_summary["net"] > 0).sum()
    losses = (day_summary["net"] < 0).sum()
    pnl_cents = day_summary["net"].sum()
    pnl_dollars = pnl_cents / 100
    avg_win = day_summary[day_summary["net"] > 0]["net"].mean() / 100 if wins else 0
    avg_loss = day_summary[day_summary["net"] < 0]["net"].mean() / 100 if losses else 0
    worst_day = day_summary["net"].min() / 100
    best_day = day_summary["net"].max() / 100
    win_rate = wins / n_days * 100 if n_days else 0
    # Sharpe approximation: assume zero-PnL days between trade days
    span_days = (day_summary["date"].max() - day_summary["date"].min()).days + 1
    daily_series = pd.Series([0] * span_days)
    for _, r in day_summary.iterrows():
        d_idx = (r["date"] - day_summary["date"].min()).days
        if 0 <= d_idx < span_days:
            daily_series.iloc[d_idx] = r["net"]
    sharpe = daily_series.mean() / daily_series.std() * np.sqrt(252) if daily_series.std() > 0 else 0
    print(f"{name}:")
    print(f"  Days traded: {n_days} ({day_summary['date'].min().date()} -> {day_summary['date'].max().date()})")
    print(f"  Wins/Losses: {wins}/{losses}  (win rate {win_rate:.0f}%)")
    print(f"  PnL: ${pnl_dollars:+.2f}")
    print(f"  Avg win: ${avg_win:+.2f}, avg loss: ${avg_loss:+.2f}")
    print(f"  Best/worst day: ${best_day:+.2f} / ${worst_day:+.2f}")
    print(f"  Sharpe (annualized, daily series): {sharpe:+.2f}")
    print()
    return day_summary


# Run full drop_lower_ask to get all 22 trade days
print("Running drop_lower_ask on full sample...")
_, pos_full = run([
    "--strategy", "wing", "--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "1.00",
    "--wing-drop-lower-ask",
], "oos_full")

# Determine the chronological median trade-date for the split
day_summary_full = pos_full.groupby("date").agg(net=("net_cents", "sum")).reset_index().sort_values("date")
n = len(day_summary_full)
split_idx = n // 2
split_date = day_summary_full["date"].iloc[split_idx]

print(f"\nSplit: {n} trade days, median trade-date = {split_date.date()}")
print(f"Early half: {day_summary_full['date'].iloc[0].date()} -> {day_summary_full['date'].iloc[split_idx-1].date()}")
print(f"Late half:  {day_summary_full['date'].iloc[split_idx].date()} -> {day_summary_full['date'].iloc[-1].date()}")
print()

# Slice the full positions by date threshold
pos_early = pos_full[pos_full["date"] < split_date]
pos_late = pos_full[pos_full["date"] >= split_date]

print("=" * 70)
print("FULL SAMPLE (drop_lower_ask, 22 days)")
print("=" * 70)
summarize("Full", pos_full)

print("=" * 70)
print("EARLY HALF (drop_lower_ask, first ~11 trade days)")
print("=" * 70)
summarize("Early", pos_early)

print("=" * 70)
print("LATE HALF (drop_lower_ask, last ~11 trade days)")
print("=" * 70)
summarize("Late", pos_late)

# Run placebo
print("Running placebo: drop_HIGHER_ask...")
_, pos_placebo = run([
    "--strategy", "wing", "--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "1.00",
    "--wing-drop-higher-ask",
], "oos_placebo")

print("=" * 70)
print("PLACEBO: drop_HIGHER_ask (keep modal + lower-ask)")
print("=" * 70)
summarize("Placebo", pos_placebo)

# Final verdict
def total_pnl(df):
    return df.groupby("date")["net_cents"].sum().sum() / 100 if not df.empty else 0

early_pnl = total_pnl(pos_early)
late_pnl = total_pnl(pos_late)
placebo_pnl = total_pnl(pos_placebo)

print()
print("=" * 70)
print("VERDICT")
print("=" * 70)
print(f"Early half PnL:  ${early_pnl:+.2f}")
print(f"Late half PnL:   ${late_pnl:+.2f}")
print(f"Placebo PnL:     ${placebo_pnl:+.2f}")
print()
if early_pnl > 0 and late_pnl > 0 and placebo_pnl < 0:
    print("STRONG: both halves positive, placebo decisively negative")
    print("-> Mechanism real and consistent.")
elif (early_pnl > 0) == (late_pnl > 0) and placebo_pnl < 0:
    print("MIXED: halves agree, placebo doesn't fail enough.")
    print("-> Some signal but variance is high.")
elif early_pnl * late_pnl < 0:
    print("UNSTABLE: halves diverge.")
    print("-> Strategy depends on a specific regime; not robust.")
elif placebo_pnl > 0:
    print("BROKEN PLACEBO: drop_higher_ask also profitable.")
    print("-> Most gain is from coverage, not the higher-ask side.")
else:
    print("Inspect manually - pattern doesn't match a clean template.")
