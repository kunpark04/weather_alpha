"""Verify §10.2 bucket-accuracy numbers, then compute agreement/market accuracy
at Kalshi bucket resolution.

Outputs the exact stats to paste into notebooks/model_v3.ipynb as a new cell.
"""

from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from forecast_alpha.pmf import parse_bucket, parse_kalshi_subtitle, bucket_prob

# ---------------------------------------------------------------------------
# A) Verify §10.2 -- fixed-edge 2F-bucket diagnostic on calibrated OOF
# ---------------------------------------------------------------------------

oof = np.load(_ROOT / "data/model_v3_artifacts/oof_bucket_probs_calib.npy")
fold = np.load(_ROOT / "data/model_v3_artifacts/oof_fold.npy")
feat = pd.read_parquet(_ROOT / "data/model_v3_artifacts/feature_df.parquet").reset_index(drop=True)
tgt = pd.read_parquet(_ROOT / "data/model_v3_artifacts/target_df.parquet").reset_index(drop=True)

valid = (fold >= 0) & ~np.isnan(oof).any(axis=1) & ~tgt["cli_high"].isna()
probs = oof[valid]
actual = tgt.loc[valid, "cli_high"].astype(int).values
dates = pd.to_datetime(feat.loc[valid, "date"]).dt.normalize()

F_GRID = np.arange(-30, 131)   # 161 buckets
assert probs.shape[1] == len(F_GRID)

# Snap to 2F pairs: low-even, high-odd
grid_lo = -30 - (-30 % 2)
bucket_for_F = (F_GRID - grid_lo) // 2
n_buckets = bucket_for_F.max() + 1

# Aggregate
agg = np.zeros((probs.shape[0], n_buckets))
np.add.at(agg.T, bucket_for_F, probs.T)
model_argmax = agg.argmax(axis=1)
actual_bucket = (actual - grid_lo) // 2

top1 = (model_argmax == actual_bucket).mean()
top3 = (np.abs(model_argmax - actual_bucket) <= 1).mean()
print("=" * 70)
print("§10.2 verification (full 2021-2026 OOF, fixed 2F-pair bucketing)")
print("=" * 70)
print(f"Days evaluated:   {len(probs)}  ({dates.min().date()} -> {dates.max().date()})")
print(f"Model top-1 (argmax bucket = actual): {top1:.1%}")
print(f"Model top-3 (within ±1 bucket):       {top3:.1%}")
print()

# ---------------------------------------------------------------------------
# B) Kalshi-bucket-resolution accuracy (only on days with Kalshi data, 2026-Q2)
#    - Buckets are the ACTUAL Kalshi spec per day (2F middles + 2 tails)
#    - Anchor: last trade <= 13:00 local
# ---------------------------------------------------------------------------

kalshi = pd.read_parquet(_ROOT / "data/kalshi_history.parquet")
kalshi["event_date"] = pd.to_datetime(kalshi["event_date"]).dt.normalize()
kalshi["timestamp"] = pd.to_datetime(kalshi["timestamp"], utc=True)

cli = pd.read_parquet(_ROOT / "data/cli_KMDW.parquet")
cli["date"] = pd.to_datetime(cli["date"]).dt.normalize()
truth = {d: int(t) for d, t in cli[["date", "max_temp_f"]].dropna().itertuples(index=False, name=None)}

date_to_idx = {d: i for i, d in enumerate(dates)}

records = []      # one row per Kalshi day with both modals + truth
for ev_date in sorted(kalshi["event_date"].unique()):
    ev = pd.Timestamp(ev_date).normalize()
    if ev not in date_to_idx or ev not in truth:
        continue
    actual_f = truth[ev]
    anchor_t = (ev.tz_localize("America/Chicago") + pd.Timedelta(hours=13)).tz_convert("UTC")
    pre = kalshi[(kalshi["event_date"] == ev) & (kalshi["timestamp"] <= anchor_t)]
    if pre.empty: continue
    latest = pre.sort_values("timestamp").groupby("ticker", as_index=False).last()

    pmf = probs[date_to_idx[ev]]
    contracts = []
    for _, row in latest.iterrows():
        spec = parse_kalshi_subtitle(row.get("subtitle"))
        if spec is None: continue
        yes_ask = min((row["yes_price_cents"] or 50) / 100 + 0.01, 0.99)
        p_model = bucket_prob(pmf, spec)
        contracts.append({"spec": spec, "yes_ask": yes_ask, "p_model": p_model, "ticker": row["ticker"]})
    if len(contracts) < 2: continue

    # Sort by lower bound for positional adjacency
    def _lo(spec):
        if spec.startswith("<="): return int(spec[2:]) - 100
        if spec.startswith(">="): return int(spec[2:])
        return int(spec.split("-")[0])
    contracts.sort(key=lambda c: _lo(c["spec"]))

    model_mod_idx = max(range(len(contracts)), key=lambda j: contracts[j]["p_model"])
    market_mod_idx = max(range(len(contracts)), key=lambda j: contracts[j]["yes_ask"])

    actual_idx = None
    for j, c in enumerate(contracts):
        pred, _ = parse_bucket(c["spec"])
        if pred(actual_f):
            actual_idx = j; break
    if actual_idx is None: continue

    records.append({
        "date": ev,
        "actual_f": actual_f,
        "actual_idx": actual_idx,
        "model_mod_idx": model_mod_idx,
        "market_mod_idx": market_mod_idx,
        "agreement": model_mod_idx == market_mod_idx,
        "model_spec": contracts[model_mod_idx]["spec"],
        "market_spec": contracts[market_mod_idx]["spec"],
        "actual_spec": contracts[actual_idx]["spec"],
        "n_buckets": len(contracts),
    })

df = pd.DataFrame(records)
print("=" * 70)
print("Kalshi-bucket-resolution accuracy (2026-03-21 -> 2026-05-26)")
print("=" * 70)
print(f"Total Kalshi days with full data:  {len(df)}")
print(f"Days where model_modal == market_modal (agreement):  {df['agreement'].sum()}  ({df['agreement'].mean():.1%})")
print()

def acc_table(label, df):
    n = len(df)
    if n == 0:
        print(f"{label}: no days"); return
    model_top1 = (df["actual_idx"] == df["model_mod_idx"]).mean()
    market_top1 = (df["actual_idx"] == df["market_mod_idx"]).mean()
    model_top3 = (np.abs(df["actual_idx"] - df["model_mod_idx"]) <= 1).mean()
    market_top3 = (np.abs(df["actual_idx"] - df["market_mod_idx"]) <= 1).mean()
    print(f"{label}  (n={n})")
    print(f"  {'':<30} {'Model':>10} {'Market':>10}")
    print(f"  {'Top-1 (modal = truth bucket)':<30} {model_top1:>10.1%} {market_top1:>10.1%}")
    print(f"  {'Top-3 (truth in modal +/- 1)':<30} {model_top3:>10.1%} {market_top3:>10.1%}")
    print()

acc_table("ALL days", df)
acc_table("Agreement days (model = market)", df[df["agreement"]])
acc_table("Disagreement days", df[~df["agreement"]])

# On agreement days, what's the average of the agreed modal accuracy?
# (model_top1 and market_top1 will be IDENTICAL because they pick the same bucket)
ag = df[df["agreement"]]
print(f"On agreement days, the joint-modal hits the truth bucket:  "
      f"{(ag['actual_idx'] == ag['model_mod_idx']).mean():.1%}")
print(f"On agreement days, truth is within +/-1 of the joint modal: "
      f"{(np.abs(ag['actual_idx'] - ag['model_mod_idx']) <= 1).mean():.1%}")
