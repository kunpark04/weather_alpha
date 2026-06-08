"""Does wing COVERAGE track market LIQUIDITY? (the suspect left standing after the weather-
volatility hypothesis was refuted in predictability_check.py). Across the 20 cities, correlate RCA
coverage with trade activity from the backfill: total trades and total volume (sum of per-trade
`count` contracts), per event-day. Liquidity is heavy-tailed (one flagship dwarfs the rest), so the
primary stat is SPEARMAN (rank) + Pearson on log10; raw Pearson is outlier-dominated and reported
only for context. Volume proxies attention/liquidity, NOT pricing sharpness directly (that needs
orderbook depth, which the trade backfill lacks).

(Distinct from scripts/liquidity_check.py, which is the P2 execution-feasibility check.)
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
        vol = float(pd.to_numeric(df["count"], errors="coerce").fillna(0).sum())
        n_days = int(df["event_date"].nunique())
        rows.append({"city": r["city"], "profitable": r["profitable"],
                     "coverage": r["coverage"], "edge": r["edge"], "n_days": n_days,
                     "trades": len(df), "volume": vol,
                     "trades_per_day": round(len(df) / n_days), "vol_per_day": round(vol / n_days)})
    d = pd.DataFrame(rows).sort_values("coverage", ascending=False)
    d["log_vol"] = np.log10(d["volume"].clip(lower=1))
    d["log_trades"] = np.log10(d["trades"].clip(lower=1))

    print("\n" + "=" * 92)
    print("COVERAGE vs MARKET LIQUIDITY  (per city, sorted by coverage)")
    print("=" * 92)
    print(f"{'city':<15}{'prof':>5}{'cover':>7}{'edge':>8}{'trades/day':>12}{'vol/day':>12}{'totVol(K)':>11}")
    for _, r in d.iterrows():
        print(f"{r['city']:<15}{('Y' if r['profitable'] else 'n'):>5}{r['coverage']:>7.0%}"
              f"{r['edge']:>+8.3f}{int(r['trades_per_day']):>12,}{int(r['vol_per_day']):>12,}"
              f"{r['volume']/1000:>11,.0f}")

    print("\nCorrelations across the 20 cities (Spearman is the headline; raw Pearson is outlier-driven):")
    for k, lab in [("log_trades", "log10(total trades)"), ("log_vol", "log10(total volume)"),
                   ("trades_per_day", "trades/day (raw)"), ("vol_per_day", "volume/day (raw)")]:
        pe, sp = _corr(d["coverage"], d[k])
        print(f"  coverage vs {lab:<22}: Spearman {sp:+.2f}   Pearson {pe:+.2f}")
    pe, sp = _corr(d["edge"], d["log_vol"])
    print(f"  edge     vs log10(total volume)  : Spearman {sp:+.2f}   Pearson {pe:+.2f}")

    prof, los = d[d["profitable"]], d[~d["profitable"]]
    print(f"\nmedian trades/day  profitable {prof['trades_per_day'].median():,.0f}  vs  losing {los['trades_per_day'].median():,.0f}")
    print(f"median volume/day  profitable {prof['vol_per_day'].median():,.0f}  vs  losing {los['vol_per_day'].median():,.0f}")


if __name__ == "__main__":
    main()
