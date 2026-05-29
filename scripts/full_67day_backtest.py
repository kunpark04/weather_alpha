"""Backtest drop_lower_ask on EVERY Kalshi day available (67 total).

Relaxes:
  - require_agreement=False (uses wing_any variant)
  - max_sum_3=10.0  (effectively disable the no-edge skip)

Reports:
  - Days where Kalshi data exists
  - Days strategy fired (and reasons for skips)
  - Per-day P&L
  - Comparison: full 67d vs filtered 22d (require_agreement=True)
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
    res = subprocess.run(cmd, capture_output=True, cwd=_ROOT, text=True)
    daily = pd.read_parquet(_ROOT / f"data/{out_name}.parquet")
    pos = pd.read_parquet(_ROOT / f"data/{out_name}_positions.parquet")
    return daily, pos, res.stdout


def summarize(name, daily, pos):
    if pos.empty:
        print(f"{name}: NO TRADES")
        return
    daily_grp = pos.groupby("date").agg(
        net=("net_cents", "sum"),
        n_legs=("ticker", "count"),
        stake=("stake_cents", "sum"),
        fees=("fee_cents", "sum"),
    ).reset_index().sort_values("date")
    n_days = len(daily_grp)
    wins = (daily_grp["net"] > 0).sum()
    losses = (daily_grp["net"] < 0).sum()
    pnl = daily_grp["net"].sum() / 100
    stake = daily_grp["stake"].sum() / 100
    fees = daily_grp["fees"].sum() / 100
    avg_win = daily_grp[daily_grp["net"] > 0]["net"].mean() / 100 if wins else 0
    avg_loss = daily_grp[daily_grp["net"] < 0]["net"].mean() / 100 if losses else 0
    best = daily_grp["net"].max() / 100
    worst = daily_grp["net"].min() / 100
    win_rate = wins / n_days * 100

    span_days = (daily_grp["date"].max() - daily_grp["date"].min()).days + 1
    s = pd.Series([0] * span_days)
    for _, r in daily_grp.iterrows():
        idx = (r["date"] - daily_grp["date"].min()).days
        s.iloc[idx] = r["net"]
    sharpe = s.mean() / s.std() * np.sqrt(252) if s.std() > 0 else 0

    print(f"{name}")
    print(f"  Days traded:   {n_days} ({daily_grp['date'].min().date()} -> {daily_grp['date'].max().date()})")
    print(f"  W/L:           {wins}/{losses}  (win rate {win_rate:.0f}%)")
    print(f"  PnL:           ${pnl:+.2f}    (stake ${stake:.2f}, fees ${fees:.2f})")
    print(f"  Avg win/loss:  ${avg_win:+.2f} / ${avg_loss:+.2f}")
    print(f"  Best/worst:    ${best:+.2f} / ${worst:+.2f}")
    print(f"  Sharpe:        {sharpe:+.2f}")
    return daily_grp


# Total Kalshi days available
k = pd.read_parquet(_ROOT / "data/kalshi_history.parquet")
k["event_date"] = pd.to_datetime(k["event_date"]).dt.normalize()
all_kalshi_days = sorted(k["event_date"].unique())
print(f"Total Kalshi days available: {len(all_kalshi_days)} "
      f"({pd.Timestamp(all_kalshi_days[0]).date()} -> {pd.Timestamp(all_kalshi_days[-1]).date()})")
print()

# Run 1: filtered (agreement required, max_sum_3=1.00) — baseline
print("=" * 78)
print("BASELINE: agreement-required drop_lower_ask (22 days)")
print("=" * 78)
_, pos_filtered, _ = run([
    "--strategy", "wing", "--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "1.00",
    "--wing-drop-lower-ask",
], "fullday_filtered")
summarize("filtered", _, pos_filtered)

# Run 2: NO-agreement, max_sum_3=10 -> fires on as many days as eligibility allows
print()
print("=" * 78)
print("UNFILTERED: wing_any + drop_lower_ask, max_sum_3=10 (fires on all eligible)")
print("=" * 78)
_, pos_full, _ = run([
    "--strategy", "wing_any", "--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "10.0",
    "--wing-drop-lower-ask",
], "fullday_unfiltered")
summarize("unfiltered drop_lower_ask", _, pos_full)

# Run 3: placebo unfiltered
print()
print("=" * 78)
print("PLACEBO UNFILTERED: wing_any + drop_HIGHER_ask, max_sum_3=10")
print("=" * 78)
_, pos_placebo, _ = run([
    "--strategy", "wing_any", "--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "10.0",
    "--wing-drop-higher-ask",
], "fullday_placebo")
summarize("unfiltered placebo", _, pos_placebo)

# Identify which Kalshi days did NOT fire under unfiltered drop_lower_ask
fired_days = set(pos_full["date"].dt.normalize().unique())
all_days = set(pd.to_datetime(all_kalshi_days))
unfired = sorted(all_days - fired_days)
print()
print("=" * 78)
print(f"Kalshi days that STILL did NOT fire (even with all filters relaxed): {len(unfired)}")
print("=" * 78)
if unfired:
    for d in unfired[:20]:
        print(f"  {d.date()}")
    if len(unfired) > 20:
        print(f"  ... ({len(unfired) - 20} more)")

# Per-day P&L table for unfiltered drop_lower_ask
print()
print("=" * 78)
print("Full per-day breakdown (unfiltered drop_lower_ask)")
print("=" * 78)
daily_pl = pos_full.groupby("date").agg(
    net=("net_cents", "sum"),
    legs=("ticker", "count"),
).reset_index().sort_values("date")
daily_pl["net$"] = daily_pl["net"] / 100
daily_pl["cumul$"] = daily_pl["net$"].cumsum()
print(f"{'#':>3} {'date':<12} {'legs':>4} {'net$':>10} {'cumul$':>10}")
for i, (_, r) in enumerate(daily_pl.iterrows(), 1):
    print(f"{i:>3} {r['date'].date()} {r['legs']:>4} {r['net$']:>+10.2f} {r['cumul$']:>+10.2f}")
