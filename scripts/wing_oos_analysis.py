"""Compare equal-payout vs prob-weighted wing on in-sample vs OOS days.

In-sample = the 7 days the strict filter (base=0.98, max_sum_3=0.97) fires on.
These were the dataset the wing strategy was developed/tuned on.

OOS = additional days the loose filter (base=0.99, max_sum_3=0.99) fires on
but the strict filter didn't. These weren't part of the design loop.
"""

from __future__ import annotations
import subprocess
import sys
from pathlib import Path
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
BACKTEST = "scripts/backtest_strategy.py"

def run(args, out_name):
    cmd = [PY, BACKTEST, "--strategy", "wing"] + args + ["--out", f"data/{out_name}.parquet"]
    subprocess.run(cmd, capture_output=True, cwd=_ROOT)
    return pd.read_parquet(_ROOT / f"data/{out_name}_positions.parquet")

print("Running backtests...")
strict_prob = run(["--wing-assumed-win-prob", "0.98", "--wing-max-ask-sum", "0.97",
                   "--wing-sizing-mode", "prob_weighted"], "strict_prob")
loose_prob = run(["--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "0.99",
                  "--wing-sizing-mode", "prob_weighted"], "loose_prob")
loose_eq = run(["--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "0.99",
                "--wing-sizing-mode", "equal_payout"], "loose_eq")

strict_dates = set(strict_prob["date"].unique())
print(f"\nStrict-filter dates (strategy-design in-sample) [{len(strict_dates)} days]:")
for d in sorted(strict_dates):
    print(f"  {pd.Timestamp(d).date()}")

new_dates = set(loose_prob["date"].unique()) - strict_dates
print(f"\nAdditional dates the loose filter fires on (strategy-OOS) [{len(new_dates)} days]:")
for d in sorted(new_dates):
    print(f"  {pd.Timestamp(d).date()}")

def day_summary(df):
    return df.groupby("date").agg(net=("net_cents", "sum"), n_legs=("ticker", "count")).reset_index()

ds_prob = day_summary(loose_prob)
ds_eq = day_summary(loose_eq)
merged = ds_prob.merge(ds_eq, on="date", suffixes=("_prob", "_eq"))
merged["in_sample"] = merged["date"].isin(strict_dates)
merged["delta"] = merged["net_prob"] - merged["net_eq"]

print("\nPer-day comparison (PnL in cents):")
print(f"  {'date':>12} {'flag':>6} {'eq_payout':>12} {'prob_w':>10} {'prob-eq':>10}")
for _, r in merged.sort_values(["in_sample", "date"], ascending=[False, True]).iterrows():
    flag = "IN" if r["in_sample"] else "OOS"
    eq_str = f"{r['net_eq']:+}"
    pw_str = f"{r['net_prob']:+}"
    d_str = f"{r['delta']:+}"
    print(f"  {str(r['date'].date()):>12} {flag:>6} {eq_str:>12} {pw_str:>10} {d_str:>10}")

print("\nAggregate splits (dollars):")
ins = merged[merged["in_sample"]]
oos = merged[~merged["in_sample"]]
total = merged

def show(name, sub):
    n = len(sub)
    eq_sum = sub["net_eq"].sum() / 100
    pw_sum = sub["net_prob"].sum() / 100
    delta_sum = sub["delta"].sum() / 100
    eq_wins = (sub["net_eq"] > 0).sum()
    pw_wins = (sub["net_prob"] > 0).sum()
    print(f"  {name:<15} N={n:>2}  eq=${eq_sum:>+7.2f} ({eq_wins}/{n}W)  prob=${pw_sum:>+7.2f} ({pw_wins}/{n}W)  delta=${delta_sum:>+7.2f}")

show("IN-SAMPLE", ins)
show("OOS NEW", oos)
show("TOTAL", total)

print("\nVerdict:")
if len(oos) > 0:
    if oos["delta"].sum() > 0:
        print(f"  Prob-weighted's advantage HOLDS on strategy-OOS days "
              f"(+${oos['delta'].sum()/100:.2f} on {len(oos)} new days)")
    elif oos["delta"].sum() < -5 * 100:
        print(f"  Prob-weighted's advantage COLLAPSES on OOS days "
              f"(${oos['delta'].sum()/100:.2f} on {len(oos)} new days) — was sample variance")
    else:
        print(f"  Prob-weighted's advantage is NEUTRAL on OOS days "
              f"(${oos['delta'].sum()/100:.2f}) — needs more data to distinguish")
else:
    print("  No new OOS days; can't disambiguate")
