"""5-year model-side stability test (2021-01 -> 2026-05).

Tests if the signals that drive our two profitable strategies generalize:
  - drop_lower_ask depends on: modal accuracy, +-1F adjacency rate, and
    confidence-floor reliability
  - hard_floor depends on: model's effectively-zero buckets actually never winning

NOTE: We cannot test the MARKET side (no Kalshi data before 2026-03), but we can
verify the model itself isn't overfit to the recent Mar-May 2026 window.
"""

from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent

oof = np.load(_ROOT / 'data/model_v3_artifacts/oof_bucket_probs_calib.npy')
fold = np.load(_ROOT / 'data/model_v3_artifacts/oof_fold.npy')
feat = pd.read_parquet(_ROOT / 'data/model_v3_artifacts/feature_df.parquet').reset_index(drop=True)
tgt = pd.read_parquet(_ROOT / 'data/model_v3_artifacts/target_df.parquet').reset_index(drop=True)

valid_mask = (fold >= 0) & ~np.isnan(oof).any(axis=1)
oof_v = oof[valid_mask]
feat_v = feat.loc[valid_mask].reset_index(drop=True)
tgt_v = tgt.loc[valid_mask].reset_index(drop=True)

# 1F-wide integer-F buckets from -30 to 130 (161 buckets)
F_GRID = np.arange(-30, 131)
assert oof_v.shape[1] == len(F_GRID)

# Truth in F
truth_f = tgt_v['cli_high'].values.astype(int)
date_arr = pd.to_datetime(feat_v['date']).dt.normalize()

# Model's modal F (argmax of PMF)
modal_f = F_GRID[np.argmax(oof_v, axis=1)]
model_modal_p = oof_v.max(axis=1)

# Errors
abs_err = np.abs(modal_f - truth_f)

# Adjacency hits
hit0 = (abs_err == 0)
hit1 = (abs_err <= 1)
hit2 = (abs_err <= 2)
hit3 = (abs_err <= 3)

# Hard-floor false positive rate: how often does truth land in a bucket the
# model assigned p < epsilon?
EPS = 1e-3
hf_fp = np.zeros(len(oof_v), dtype=bool)
for i in range(len(oof_v)):
    t_idx = int(truth_f[i] - (-30))      # truth bucket index
    if 0 <= t_idx < 161:
        hf_fp[i] = oof_v[i, t_idx] < EPS
    else:
        hf_fp[i] = True  # truth outside grid - shouldn't happen for KMDW

# Sum-2 confidence (modal F +- 0F window = exact, +-1F = 3-bin sum):
# What drives the wing strategy is the 3-bin sum centered on modal (analogous to
# a "modal bucket" in 3F-wide Kalshi). Compute this:
sum_3bin = np.zeros(len(oof_v))
for i in range(len(oof_v)):
    j = int(modal_f[i] - (-30))
    lo = max(0, j - 1); hi = min(161, j + 2)
    sum_3bin[i] = oof_v[i, lo:hi].sum()

# Year column
year = date_arr.dt.year.values

# Per-year table
print("=" * 90)
print("Per-year model signal stability (KMDW, model v3, OOF, walk-forward)")
print("=" * 90)
print(f"{'year':<6} {'days':>5} {'MAE':>6} {'hit=0F':>8} {'<=1F':>7} {'<=2F':>7} {'<=3F':>7} "
      f"{'mean p_mod':>11} {'mean s3':>9} {'HF_fp':>7}")
print("-" * 90)
years = sorted(set(year))
for y in years:
    m = year == y
    n = m.sum()
    if n == 0: continue
    print(f"{y:<6} {n:>5} {abs_err[m].mean():>6.2f} "
          f"{hit0[m].mean()*100:>7.1f}% {hit1[m].mean()*100:>6.1f}% "
          f"{hit2[m].mean()*100:>6.1f}% {hit3[m].mean()*100:>6.1f}% "
          f"{model_modal_p[m].mean():>11.3f} {sum_3bin[m].mean():>9.3f} "
          f"{hf_fp[m].mean()*100:>6.2f}%")

# Overall
print("-" * 90)
m_all = np.ones(len(oof_v), dtype=bool)
print(f"{'ALL':<6} {m_all.sum():>5} {abs_err.mean():>6.2f} "
      f"{hit0.mean()*100:>7.1f}% {hit1.mean()*100:>6.1f}% "
      f"{hit2.mean()*100:>6.1f}% {hit3.mean()*100:>6.1f}% "
      f"{model_modal_p.mean():>11.3f} {sum_3bin.mean():>9.3f} "
      f"{hf_fp.mean()*100:>6.2f}%")

# Half-by-half OOS-style
print()
print("=" * 90)
print("HALF-BY-HALF OOS-style robustness")
print("=" * 90)
mid_date = pd.Timestamp('2023-09-01')   # roughly half
early = date_arr < mid_date
late = ~early
print(f"  EARLY:  {early.sum()} days ({date_arr[early].min().date()} -> {date_arr[early].max().date()})")
print(f"  LATE :  {late.sum()} days ({date_arr[late].min().date()} -> {date_arr[late].max().date()})")
print()
print(f"{'period':<10} {'MAE':>6} {'hit=0F':>8} {'<=1F':>7} {'<=2F':>7} {'<=3F':>7} "
      f"{'p_modal':>9} {'s3':>7} {'HF_fp':>7}")
print("-" * 75)
for label, m in [("EARLY", early), ("LATE", late)]:
    print(f"{label:<10} {abs_err[m].mean():>6.2f} "
          f"{hit0[m].mean()*100:>7.1f}% {hit1[m].mean()*100:>6.1f}% "
          f"{hit2[m].mean()*100:>6.1f}% {hit3[m].mean()*100:>6.1f}% "
          f"{model_modal_p[m].mean():>9.3f} {sum_3bin[m].mean():>7.3f} "
          f"{hf_fp[m].mean()*100:>6.2f}%")

# Recent-vs-rest: is 2026-Q2 (the actual Kalshi window) anomalous?
print()
print("=" * 90)
print("Kalshi window (2026-03-21 -> 2026-05-21) vs rest of OOF")
print("=" * 90)
kalshi_win = (date_arr >= pd.Timestamp('2026-03-21')) & (date_arr <= pd.Timestamp('2026-05-21'))
rest = ~kalshi_win
print(f"{'period':<25} {'n':>5} {'MAE':>6} {'hit=0F':>8} {'<=1F':>7} {'<=2F':>7} {'p_modal':>9} {'s3':>7} {'HF_fp':>7}")
print("-" * 85)
for label, m in [("Kalshi window (2026-Q2)", kalshi_win), ("Rest of OOF", rest)]:
    if m.sum() == 0: continue
    print(f"{label:<25} {m.sum():>5} {abs_err[m].mean():>6.2f} "
          f"{hit0[m].mean()*100:>7.1f}% {hit1[m].mean()*100:>6.1f}% "
          f"{hit2[m].mean()*100:>6.1f}% "
          f"{model_modal_p[m].mean():>9.3f} {sum_3bin[m].mean():>7.3f} "
          f"{hf_fp[m].mean()*100:>6.2f}%")

# Same season comparison (MAM only)
print()
print("=" * 90)
print("APPLES-to-APPLES: MAM (Mar/Apr/May) only across years")
print("=" * 90)
mam = date_arr.dt.month.isin([3, 4, 5]).values
print(f"{'year (MAM)':<12} {'n':>5} {'MAE':>6} {'hit=0F':>8} {'<=1F':>7} {'<=2F':>7} "
      f"{'p_modal':>9} {'s3':>7} {'HF_fp':>7}")
print("-" * 80)
for y in years:
    m = (year == y) & mam
    if m.sum() == 0: continue
    print(f"{y:<12} {m.sum():>5} {abs_err[m].mean():>6.2f} "
          f"{hit0[m].mean()*100:>7.1f}% {hit1[m].mean()*100:>6.1f}% "
          f"{hit2[m].mean()*100:>6.1f}% "
          f"{model_modal_p[m].mean():>9.3f} {sum_3bin[m].mean():>7.3f} "
          f"{hf_fp[m].mean()*100:>6.2f}%")
