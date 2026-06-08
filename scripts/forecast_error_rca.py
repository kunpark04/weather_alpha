"""Forecast-error RCA: WHY do low-coverage cities miss? Decompose the 1 PM modal-vs-winner error
into bias / spread / tails and correlate each with coverage across cities.

For each city-day: modal = highest-priced bucket at 1 PM; winner = settled bucket. error (degF) =
winner_midpoint - modal_midpoint (buckets are 2 degF, so |error|=2 is one bucket off, >=4 is two+).
  bias   = mean(error)      -> systematic offset (calibration / strike placement)
  spread = std(error)       -> random scatter      (forecastability)
  p_tail = P(|error| >= 4)  -> big misses          (regime / frontal volatility)
Whichever tracks coverage across the 20 cities is the dominant failure mode.

Truth = Kalshi settled bucket (67-day backfill, BUCKET resolution; tails T## use the censored
threshold midpoint -> minor noise on rare extreme days). This is the cross-city test doable NOW;
a finer/longer version needs external per-city NOAA actuals (only Chicago/KMDW is on disk).
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from scripts.rca_profitable_vs_losing import rca_city
from weather_alpha.config import load_config


def midpoint(ticker: str):
    suf = ticker.split("-")[-1]
    if not suf:
        return None
    try:
        return float(suf[1:]) if suf[0] in "BbTt" else None
    except ValueError:
        return None


def _corr(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 3:
        return float("nan"), float("nan")
    pe = float(np.corrcoef(a[m], b[m])[0, 1])
    sp = float(np.corrcoef(pd.Series(a[m]).rank(), pd.Series(b[m]).rank())[0, 1])
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
        if not r or r["fired"] < 8:          # drop LA (fires ~1 day -> coverage is meaningless)
            continue
        tz = ZoneInfo(CITY[s][1])
        errs = []
        for ev, day in df.groupby("event_date"):
            wk = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
            if len(wk) != 1:
                continue
            wmid = midpoint(wk[0])
            anchor = pd.Timestamp(ev.year, ev.month, ev.day, 13, tz=tz).tz_convert("UTC")
            pre = day[day["timestamp"] <= anchor].dropna(subset=["yes_price_cents"])
            if pre.empty:
                continue
            last = pre.sort_values("timestamp").groupby("ticker")["yes_price_cents"].last()
            if last.empty:
                continue
            mmid = midpoint(last.idxmax())
            if wmid is None or mmid is None:
                continue
            errs.append(wmid - mmid)
        if len(errs) < 10:
            continue
        e = np.array(errs)
        rows.append({"city": CITY[s][0], "cov": r["coverage"], "n": len(e),
                     "bias": float(e.mean()), "abs_bias": abs(float(e.mean())),
                     "spread": float(e.std()),
                     "p_modal": float((e == 0).mean()),
                     "p_tail": float((np.abs(e) >= 4).mean())})
    d = pd.DataFrame(rows).sort_values("cov", ascending=False)

    print("=" * 86)
    print("FORECAST-ERROR DECOMPOSITION  (1 PM modal vs settled winner, degF; sorted by coverage)")
    print("=" * 86)
    print(f"{'city':<14}{'cover':>7}{'n':>5}{'bias':>8}{'spread':>8}{'modal%':>8}{'tail%':>8}")
    for _, r in d.iterrows():
        print(f"{r['city']:<14}{r['cov']:>7.0%}{int(r['n']):>5}{r['bias']:>+8.2f}"
              f"{r['spread']:>8.2f}{r['p_modal']:>8.0%}{r['p_tail']:>8.0%}")

    print("\nCROSS-CITY: what tracks coverage? (negative = that error mode lowers coverage)")
    for k, lab in [("spread", "spread  (forecastability)"), ("abs_bias", "|bias|  (calibration)"),
                   ("p_tail", "tail%   (regime/big misses)"), ("p_modal", "modal%  (modal-correct)")]:
        pe, sp = _corr(d["cov"], d[k])
        print(f"  coverage vs {lab:<28}: Spearman {sp:+.2f}   Pearson {pe:+.2f}")

    win = d[d["cov"] >= 0.85]; los = d[d["cov"] < 0.85]
    print(f"\ngroup means      hi-cov (>=85%, n={len(win)})   vs   lo-cov (<85%, n={len(los)})")
    for k in ["spread", "abs_bias", "p_tail", "p_modal"]:
        print(f"  {k:<10} {win[k].mean():>8.2f}   {los[k].mean():>8.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
