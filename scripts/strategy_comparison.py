"""Comprehensive comparison table for all production-candidate strategies.

Reports: time span, days traded, win rate (day + position), avg win, avg loss,
PnL, Sharpe, max DD. Run each variant and aggregate.
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


def run_and_load(args, tag):
    out = f"data/cmp_{tag}.parquet"
    cmd = [PY, BACKTEST] + args + ["--out", out]
    subprocess.run(cmd, capture_output=True, cwd=_ROOT)
    daily = pd.read_parquet(_ROOT / out)
    pos = pd.read_parquet(_ROOT / out.replace(".parquet", "_positions.parquet"))
    return daily, pos


def summarize(name, daily, pos, total_eligible_days):
    if daily.empty:
        return {"name": name, "fires": 0, "n_positions": 0}
    trade_days = daily[daily["n_targets"] > 0] if "n_targets" in daily.columns else daily

    n_days = len(trade_days)
    span_days = (daily["date"].max() - daily["date"].min()).days + 1 if len(daily) else 0
    n_pos = len(pos)
    total_pnl = pos["net_cents"].sum() if not pos.empty else 0
    total_fees = pos["fee_cents"].sum() if not pos.empty else 0

    # Day-level win rate
    day_win = (trade_days["day_pnl_cents"] > 0).sum()
    day_loss = (trade_days["day_pnl_cents"] < 0).sum()
    day_flat = (trade_days["day_pnl_cents"] == 0).sum()
    day_win_rate = day_win / max(n_days, 1) * 100

    # Position-level win rate (per ticker leg)
    pos_win = (pos["net_cents"] > 0).sum() if not pos.empty else 0
    pos_loss = (pos["net_cents"] < 0).sum() if not pos.empty else 0
    pos_win_rate = pos_win / max(n_pos, 1) * 100

    avg_win_pos = pos[pos["net_cents"] > 0]["net_cents"].mean() / 100 if pos_win else 0
    avg_loss_pos = pos[pos["net_cents"] < 0]["net_cents"].mean() / 100 if pos_loss else 0

    # Day-level avg win/loss
    day_pnl = trade_days["day_pnl_cents"]
    avg_win_day = day_pnl[day_pnl > 0].mean() / 100 if day_win else 0
    avg_loss_day = day_pnl[day_pnl < 0].mean() / 100 if day_loss else 0

    # Sharpe (daily PnL across ALL days in window, not just trading days)
    pnl_series = daily.set_index("date")["day_pnl_cents"].reindex(
        pd.date_range(daily["date"].min(), daily["date"].max(), freq="D"),
        fill_value=0,
    )
    sharpe = pnl_series.mean() / pnl_series.std() * np.sqrt(252) if pnl_series.std() > 0 else 0

    # Max drawdown
    equity = pnl_series.cumsum() / 100
    peak = equity.cummax()
    dd = (equity - peak).min()

    return {
        "name":              name,
        "span_days":         span_days,
        "eligible_days":     total_eligible_days,
        "trade_days":        n_days,
        "fire_rate":         n_days / max(total_eligible_days, 1) * 100,
        "n_positions":       n_pos,
        "total_pnl":         total_pnl / 100,
        "total_fees":        total_fees / 100,
        "day_W":             day_win,
        "day_L":             day_loss,
        "day_win_rate":      day_win_rate,
        "pos_W":             pos_win,
        "pos_L":             pos_loss,
        "pos_win_rate":      pos_win_rate,
        "avg_day_win":       avg_win_day,
        "avg_day_loss":      avg_loss_day,
        "avg_pos_win":       avg_win_pos,
        "avg_pos_loss":      avg_loss_pos,
        "sharpe":            sharpe,
        "max_dd_usd":        dd,
    }


# Total dates in the window we're testing
# (the loosest filters fire on 20 days for wing_any; 34 for hard_floor; 60 total days have any data)
ELIGIBLE = 60

variants = [
    ("hard_floor",              ["--strategy", "hard_floor"]),
    ("wing eq-payout (default)", ["--strategy", "wing", "--wing-base-rate", "0.98", "--wing-max-sum-3", "0.97"]),
    ("wing eq-payout (loose)",  ["--strategy", "wing", "--wing-base-rate", "0.99", "--wing-max-sum-3", "1.00"]),
    ("wing prob-weighted",      ["--strategy", "wing", "--wing-base-rate", "0.99", "--wing-max-sum-3", "1.00", "--wing-sizing-mode", "prob_weighted"]),
    ("wing_any (no agreement)", ["--strategy", "wing_any", "--wing-base-rate", "0.99", "--wing-max-sum-3", "1.00"]),
    ("joint_kelly (reference)", ["--strategy", "joint_kelly"]),
]

print("Running all variants...")
rows = []
for name, args in variants:
    daily, pos = run_and_load(args, name.split()[0].lower().replace("(", "").replace(")", ""))
    rows.append(summarize(name, daily, pos, ELIGIBLE))

# Format output
print(f"\n{'='*128}")
print("STRATEGY COMPARISON  --  full backtest window: 2026-03-22 -> 2026-05-21 (61 OOF days, ~60 with Kalshi)")
print(f"{'='*128}\n")

print(f"{'Strategy':<28} {'span':>5} {'fired':>7} {'fire%':>6} {'pos':>4} "
      f"{'day W/L':>10} {'win%':>5} {'pos W/L':>10} {'pwin%':>5} "
      f"{'PnL$':>8} {'fee$':>6} {'Sharpe':>7}")
print("-" * 128)
for r in rows:
    day_wl = f"{r['day_W']}/{r['day_L']}"
    pos_wl = f"{r['pos_W']}/{r['pos_L']}"
    print(f"{r['name']:<28} {r['span_days']:>5} {r['trade_days']:>7} "
          f"{r['fire_rate']:>5.0f}% {r['n_positions']:>4} "
          f"{day_wl:>10} {r['day_win_rate']:>4.0f}% "
          f"{pos_wl:>10} {r['pos_win_rate']:>4.0f}% "
          f"{r['total_pnl']:>+8.2f} {r['total_fees']:>+6.2f} {r['sharpe']:>+7.2f}")

print()
print("Per-trade / per-day averages (USD):")
print(f"{'Strategy':<28} {'avg DAY win':>12} {'avg DAY loss':>13} {'avg POS win':>13} {'avg POS loss':>13} {'max DD':>10}")
# Avg-loss columns will print negative naturally
print("-" * 100)
for r in rows:
    print(f"{r['name']:<28} {r['avg_day_win']:>+12.2f} {r['avg_day_loss']:>+13.2f} "
          f"{r['avg_pos_win']:>+13.2f} {r['avg_pos_loss']:>+13.2f} {r['max_dd_usd']:>+10.2f}")

print()
print("Notes:")
print("  - 'span' = days from first to last trade day inclusive")
print("  - 'fired' = days the strategy actually opened positions")
print("  - 'day W/L' / 'pos W/L' track positive vs negative net at the day and per-position level")
print("  - 'avg POS win/loss' = mean net per individual contract-position (wing has 3 legs per day)")
print("  - hard_floor positions are individually-priced NO trades; 1 day can have 1-5 positions")
print("  - max DD measured in USD on $1000 starting bankroll, daily-mark-to-equity (not intraday)")
