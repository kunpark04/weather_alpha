"""(b) Predictability test: does a city's wing COVERAGE track the volatility of its settled daily
high? Across the 20 cities, correlate RCA coverage (winner inside the 2-leg wing on fired days)
with two volatility measures of the settled high series:
  - high_std : std of the settled daily high (level dispersion)
  - dod_std  : std of the day-over-day CHANGE in settled high (how much it jumps day to day)

Settled high proxy = the winning bucket's ticker number (B49.5 -> 49.5 midpoint; tails T## -> the
threshold, a censored proxy -- rare). A strong NEGATIVE corr (low volatility -> high coverage)
supports the 'it's weather predictability' root cause; a weak corr points at market pricing quality
/ bucket placement instead. Buckets are a uniform 2 F wide for all cities, so width is not a confound.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from scripts.rca_profitable_vs_losing import rca_city
from weather_alpha.config import load_config


def settled_high_series(df: pd.DataFrame) -> pd.Series:
    rec = []
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        suf = w[0].split("-")[-1]
        try:
            rec.append((pd.Timestamp(ev), float(suf[1:])))   # B49.5->49.5 ; T89->89
        except ValueError:
            continue
    rec.sort()
    return pd.Series([h for _, h in rec])


def _corr(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    pe = float(np.corrcoef(a, b)[0, 1])
    sp = float(np.corrcoef(pd.Series(a).rank(), pd.Series(b).rank())[0, 1])
    return pe, sp


def main():
    cfg = load_config()
    rows = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        r = rca_city(df, s, cfg)
        if not r:
            continue
        hs = settled_high_series(df)
        dod = hs.diff().dropna()
        rows.append({"city": r["city"], "profitable": r["profitable"],
                     "coverage": r["coverage"], "edge": r["edge"], "n": len(hs),
                     "high_std": round(float(hs.std()), 2),
                     "dod_std": round(float(dod.std()), 2),
                     "dod_mad": round(float(dod.abs().mean()), 2)})
    d = pd.DataFrame(rows).sort_values("coverage", ascending=False)

    print("\n" + "=" * 78)
    print("COVERAGE vs SETTLED-HIGH VOLATILITY  (per city, sorted by coverage)")
    print("=" * 78)
    print(f"{'city':<15}{'prof':>5}{'cover':>7}{'edge':>8}{'n':>4}{'high_std':>10}{'dod_std':>9}{'dod_mad':>9}")
    for _, r in d.iterrows():
        print(f"{r['city']:<15}{('Y' if r['profitable'] else 'n'):>5}{r['coverage']:>7.0%}"
              f"{r['edge']:>+8.3f}{int(r['n']):>4}{r['high_std']:>10.2f}{r['dod_std']:>9.2f}{r['dod_mad']:>9.2f}")

    print("\nCorrelations across the 20 cities:")
    for k in ["high_std", "dod_std", "dod_mad"]:
        pe, sp = _corr(d["coverage"], d[k])
        print(f"  coverage vs {k:<9}: Pearson {pe:+.2f}   Spearman {sp:+.2f}")
    pe, sp = _corr(d["edge"], d["dod_std"])
    print(f"  edge     vs dod_std  : Pearson {pe:+.2f}   Spearman {sp:+.2f}")

    prof = d[d["profitable"]]; los = d[~d["profitable"]]
    print(f"\nmean dod_std  profitable {prof['dod_std'].mean():.2f}  vs  losing {los['dod_std'].mean():.2f}")
    print(f"mean high_std profitable {prof['high_std'].mean():.2f}  vs  losing {los['high_std'].mean():.2f}")


if __name__ == "__main__":
    main()
