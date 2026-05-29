"""Why is v3 (1 PM anchor) so much better than v4 (midnight anchor)?

Quantifies, on the SAME OOF days, (1) the probabilistic-skill gap and (2) the
information mechanism behind it — what the 1 PM anchor can see that midnight
cannot: the 12Z HRRR run, near-peak observations, and a running-max floor.

Run:  python scripts/v3_v4_why.py
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

F = np.arange(-30, 131)


def load(tag):
    d = _ROOT / f"data/model_{tag}_artifacts"
    pmf = np.load(d / "oof_bucket_probs_calib.npy")
    fold = np.load(d / "oof_fold.npy")
    feat = pd.read_parquet(d / "feature_df.parquet").reset_index(drop=True)
    tgt = pd.read_parquet(d / "target_df.parquet").reset_index(drop=True)
    valid = (fold >= 0) & ~np.isnan(pmf).any(axis=1) & ~tgt["cli_high"].isna()
    feat = feat[valid].reset_index(drop=True)
    feat["date"] = pd.to_datetime(feat["date"]).dt.normalize()
    feat["cli_high"] = tgt.loc[valid, "cli_high"].astype(float).values
    return pmf[valid], feat


def crps(pmf, actual):
    cdf = np.cumsum(pmf, axis=1)
    out = np.empty(len(actual))
    for i, a in enumerate(actual):
        step = (F >= int(round(a))).astype(float)
        out[i] = np.sum((cdf[i] - step) ** 2)   # grid spacing = 1 °F
    return out


def quantile_F(pmf, q):
    cdf = np.cumsum(pmf, axis=1)
    return F[np.argmax(cdf >= q, axis=1)]


def skill_block(tag, pmf, feat):
    actual = feat["cli_high"].values
    c = crps(pmf, actual)
    width = quantile_F(pmf, 0.90) - quantile_F(pmf, 0.10)
    med = quantile_F(pmf, 0.50)
    peakp = pmf.max(axis=1)
    print(f"  {tag:<14}{c.mean():>9.3f}{width.mean():>11.1f}{peakp.mean():>11.1%}"
          f"{np.abs(med - actual).mean():>12.2f}")
    return c


def main():
    v3pmf, v3 = load("v3")
    v4pmf, v4 = load("v4")
    # align on common OOF dates
    common = sorted(set(v3["date"]) & set(v4["date"]))
    i3 = v3.set_index("date").index.get_indexer(common)
    i4 = v4.set_index("date").index.get_indexer(common)
    v3c, v3pmf = v3.iloc[i3].reset_index(drop=True), v3pmf[i3]
    v4c, v4pmf = v4.iloc[i4].reset_index(drop=True), v4pmf[i4]
    actual = v3c["cli_high"].values
    assert np.allclose(actual, v4c["cli_high"].values), "truth misaligned"

    print("=" * 70)
    print(f"PROBABILISTIC SKILL on {len(common)} common OOF days "
          f"({common[0].date()} → {common[-1].date()})")
    print("=" * 70)
    print(f"  {'model':<14}{'CRPS °F':>9}{'80%CI °F':>11}{'peak P':>11}{'|med-truth|':>12}")
    print("   (lower CRPS / narrower CI / higher peak / lower error = better)")
    skill_block("v3 (1 PM)", v3pmf, v3c)
    skill_block("v4 (midnight)", v4pmf, v4c)

    print("\n" + "=" * 70)
    print("INFORMATION MECHANISM — what 1 PM sees that midnight cannot")
    print("=" * 70)

    # 1) HRRR afternoon-high forecast (v3 has it; v4 has none)
    if "hrrr_t2m_max_peak" in v3c.columns:
        h = v3c["hrrr_t2m_max_peak"].astype(float).values
        ok = ~np.isnan(h)
        mae = np.abs(h[ok] - actual[ok]).mean()
        within2 = (np.abs(h[ok] - actual[ok]) <= 2).mean()
        print(f"\n  [HRRR] v3's hrrr_t2m_max_peak vs truth:  MAE {mae:.2f}°F, "
              f"within ±2°F {within2:.0%}  (n={ok.sum()})")
        print(f"         v4 has NO HRRR features — the 12Z run isn't published by midnight.")

    # 2) running-max-at-T: how much of the high is already observed by the anchor
    for tag, dfc in [("v3 (1 PM)", v3c), ("v4 (midnight)", v4c)]:
        if "running_max_F_T" in dfc.columns:
            rm = dfc["running_max_F_T"].astype(float).values
            ok = ~np.isnan(rm)
            gap = actual[ok] - rm[ok]            # °F the high exceeds what's seen by T
            already = (rm[ok] >= actual[ok] - 1).mean()  # high essentially already reached
            print(f"\n  [running max @ T] {tag}: mean(cli_high - running_max_F_T) = {gap.mean():+.1f}°F "
                  f"(median {np.median(gap):+.0f}); high already reached by T on {already:.0%} of days")

    # 3) persistence (what midnight effectively leans on)
    yday = v4c["cli_high_yesterday"].astype(float).values
    ok = ~np.isnan(yday)
    print(f"\n  [persistence] |cli_high - cli_high_yesterday| MAE = {np.abs(actual[ok]-yday[ok]).mean():.2f}°F "
          f"(the day-over-day swing a midnight model must guess blind)")

    # 4) seasonal CRPS split
    print("\n" + "=" * 70)
    print("CRPS by season (spring = frontal season = hardest for a blind model)")
    print("=" * 70)
    c3, c4 = crps(v3pmf, actual), crps(v4pmf, actual)
    seas = pd.to_datetime(pd.Series(common)).dt.month.map(
        {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
         6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}).values
    print(f"  {'season':<8}{'n':>5}{'v3 CRPS':>10}{'v4 CRPS':>10}{'v4/v3':>8}")
    for s in ["DJF", "MAM", "JJA", "SON"]:
        m = seas == s
        if m.sum():
            print(f"  {s:<8}{m.sum():>5}{c3[m].mean():>10.3f}{c4[m].mean():>10.3f}"
                  f"{c4[m].mean()/c3[m].mean():>8.2f}x")


if __name__ == "__main__":
    main()
