"""Per-loss-day sizing breakdown.

Decomposes total daily stake into:
  - sum_asks (drives Kelly: lower = bigger position)
  - throttle multiplier (peak_P or HRRR-gap halves it)
  - bankroll at time of trade (compounding)
  - per_contract cap binding

Compares loss days against winning days to show what made losses bigger.
"""

from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

pos = pd.read_parquet(_ROOT / "data/mw_droplow_positions.parquet")
pos["date"] = pd.to_datetime(pos["date"]).dt.normalize()
daily = pos.groupby("date").agg(
    n_legs=("ticker", "count"),
    stake=("stake_cents", "sum"),
    contracts=("contracts", "sum"),
    fees=("fee_cents", "sum"),
    net=("net_cents", "sum"),
).reset_index().sort_values("date")
daily["stake$"] = daily["stake"] / 100
daily["net$"] = daily["net"] / 100
daily["cum$"] = daily["net$"].cumsum()
# Use first leg's stored values directly (columns flattened in positions parquet)
first_leg = (pos.sort_values("date")
                .drop_duplicates("date", keep="first")
                .set_index("date")[["kelly_f", "confidence", "bankroll_pre"]])
daily = daily.merge(first_leg, left_on="date", right_index=True, how="left")
daily.rename(columns={"bankroll_pre": "bankroll_pre$"}, inplace=True)
# Compute sum_asks across the day's legs
sum_ask = pos.groupby("date")["entry_cents"].sum() / 100
daily = daily.merge(sum_ask.rename("sum_asks"), left_on="date", right_index=True, how="left")
daily["stake_pct_of_bankroll"] = daily["stake$"] / daily["bankroll_pre$"] * 100

print("=" * 110)
print("market_wing + drop_lower_ask  -- per-day sizing breakdown")
print("=" * 110)
print(f"{'date':<12} {'br_pre$':>9} {'sum_ask':>8} {'kelly_f':>8} "
      f"{'stake$':>9} {'%br':>6} {'contracts':>10} {'net$':>10}")
print("-" * 110)
for _, r in daily.iterrows():
    print(f"{r['date'].date()} {r['bankroll_pre$']:>9.2f} "
          f"{r['sum_asks']:>8.3f} {r['kelly_f']:>8.3f} "
          f"{r['stake$']:>9.2f} {r['stake_pct_of_bankroll']:>5.1f}% {int(r['contracts']):>10} "
          f"{r['net$']:>+10.2f}")

# Loss-vs-win comparison
print()
print("=" * 80)
print("Aggregate: losses vs wins")
print("=" * 80)
losses = daily[daily["net$"] < 0]
wins = daily[daily["net$"] > 0]
print(f"{'metric':<28} {'losses':>12} {'wins':>12}")
print("-" * 56)
for label, col in [
    ("avg stake $", "stake$"),
    ("avg stake % bankroll", "stake_pct_of_bankroll"),
    ("avg sum_asks", "sum_asks"),
    ("avg kelly_f (full)", "kelly_f"),
    ("avg bankroll before", "bankroll_pre$"),
    ("avg contracts/day", "contracts"),
]:
    lv = losses[col].mean()
    wv = wins[col].mean()
    print(f"{label:<28} {lv:>12.3f} {wv:>12.3f}")
