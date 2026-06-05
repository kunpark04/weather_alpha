"""Significance of Chicago's OUT-OF-SAMPLE wing edge -- the single number the live deployment rests on.

OOS = the held-out last 20% of Chicago's deep (2023+) history. The 80:20 split flagged Chicago at
+8c/position / +$12 over its OOS fired days. Is that distinguishable from zero? Three angles:
  1. one-sample t-test on OOS fired-DAY PnL (the independent unit; days, not positions)
  2. bootstrap 95% CI on total OOS PnL + one-sided p(total <= 0)
  3. signal vs placebo OOS (drop_lower vs drop_higher on the SAME OOS days) -- Welch t + gap
For context it also prints the in-sample edge (is OOS consistent with IS, or a fluke either way).
Flat $2.50, live config. CAVEAT: n is small (~75 fired days); this bounds the noise, it can't
manufacture power that 8 months of OOS doesn't contain.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import run_one
from scripts.modal_rank_multicity import load_city
from weather_alpha.config import load_config

SERIES = "KXHIGHCHI"
SPLIT = 0.80
B = 50000


def fired_pnl(df, cfg, *, drop_lower, drop_higher):
    """(IS fired-day PnL$, OOS fired-day PnL$, cut date) for the flat wing."""
    rows = run_one(df, SERIES, cfg, sizing="flat", drop_lower=drop_lower, drop_higher=drop_higher)
    dates = sorted(rows["date"].unique())
    cut = dates[int(len(dates) * SPLIT)]
    fired = rows[rows["traded"]]
    is_p = fired[fired["date"] < cut]["pnl_c"].to_numpy(float) / 100.0
    oos_p = fired[fired["date"] >= cut]["pnl_c"].to_numpy(float) / 100.0
    return is_p, oos_p, pd.Timestamp(cut).date()


def main():
    cfg = load_config()
    df = load_city(SERIES)
    is_sig, oos_sig, cut = fired_pnl(df, cfg, drop_lower=True, drop_higher=False)
    _, oos_plac, _ = fired_pnl(df, cfg, drop_lower=False, drop_higher=True)

    n = len(oos_sig)
    tot, mean = oos_sig.sum(), oos_sig.mean()
    sd = oos_sig.std(ddof=1)
    se = sd / np.sqrt(n)
    t = mean / se if se > 0 else float("nan")

    # p-values: scipy if present, else normal approx
    p_t = p_welch = t_welch = None
    try:
        from scipy import stats
        p_t = float(stats.t.sf(abs(t), n - 1) * 2)
        t_welch, p_welch = stats.ttest_ind(oos_sig, oos_plac, equal_var=False)
    except Exception:
        from math import erf, sqrt
        p_t = 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2))))

    rng = np.random.default_rng(12345)
    boot = oos_sig[rng.integers(0, n, size=(B, n))].sum(axis=1)
    ci = np.percentile(boot, [2.5, 97.5])
    p_boot = float((boot <= 0).mean())                 # one-sided: P(total <= 0) under resampling

    print("=" * 78)
    print("CHICAGO OUT-OF-SAMPLE EDGE -- SIGNIFICANCE  (flat $2.50, drop_lower_ask)")
    print("=" * 78)
    print(f"split cut (last 20% held out): {cut}")
    print(f"in-sample : {len(is_sig):>3} fired days, total ${is_sig.sum():+.2f}, "
          f"mean ${is_sig.mean():+.3f}/day")
    print(f"OUT-OF-SAMPLE: {n} fired days, total ${tot:+.2f}, mean ${mean:+.3f}/day, "
          f"sd ${sd:.2f}, se ${se:.3f}")
    print(f"  per-position edge: {tot*100/(2*n):+.1f} c   (2 legs/day)")

    print("\n(1) one-sample t-test  H0: mean OOS daily PnL = 0")
    print(f"      t({n-1}) = {t:+.2f}   two-sided p = {p_t:.3f}")
    print("(2) bootstrap on total OOS PnL  (50k resamples)")
    print(f"      95% CI = [${ci[0]:+.2f}, ${ci[1]:+.2f}]   one-sided p(total<=0) = {p_boot:.3f}")
    print("(3) signal vs placebo OOS  (same {} OOS days; drop_lower vs drop_higher)".format(
        max(n, len(oos_plac))))
    print(f"      signal total ${oos_sig.sum():+.2f}  vs  placebo total ${oos_plac.sum():+.2f}  "
          f"(gap ${oos_sig.sum()-oos_plac.sum():+.2f})")
    if p_welch is not None:
        print(f"      Welch t = {float(t_welch):+.2f}   two-sided p = {float(p_welch):.4f}")

    print("\nREAD: (1)/(2) test whether Chicago's OWN OOS edge clears zero (small n -> wide band).")
    print("(3) tests the far stronger claim -- that the drop_lower DIRECTION beats its placebo OOS.")
    print("A tight (3) with a borderline (1)/(2) = 'mechanism real, absolute edge thin but positive'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
