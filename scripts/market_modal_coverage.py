"""Market-only modal coverage: how often settlement lands in the market's
top-1 / top-2 / top-3 buckets (ranked by yes-price), at the midnight vs the
1 PM snapshot.

No model involved — this is purely the Kalshi order book's own ranking. "1st
modal" = highest-ask bucket; coverage is CUMULATIVE (top-2 = truth in either of
the two highest-ask buckets; top-3 = in any of the three highest).

Computed on the SAME set of days at both snapshots (days with a valid book at
BOTH midnight and 1 PM, and known truth) so the only variable is the clock.

Usage:  python scripts/market_modal_coverage.py
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from weather_alpha.pmf import parse_bucket, parse_kalshi_subtitle

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _lo(s):
    if s.startswith("<="):
        return int(s[2:]) - 100
    if s.startswith(">="):
        return int(s[2:])
    return int(s.split("-")[0])


def snapshot(kalshi, ev, hour):
    at = (ev.tz_localize("America/Chicago") + pd.Timedelta(hours=hour)).tz_convert("UTC")
    pre = kalshi[(kalshi["event_date"] == ev) & (kalshi["timestamp"] <= at)]
    if pre.empty:
        return None
    latest = pre.sort_values("timestamp").groupby("ticker", as_index=False).last()
    cs = []
    for _, r in latest.iterrows():
        spec = parse_kalshi_subtitle(r.get("subtitle"))
        if spec is None:
            continue
        cs.append((spec, (r["yes_price_cents"] or 50) / 100))
    if len(cs) < 2:
        return None
    cs.sort(key=lambda x: _lo(x[0]))  # positional order (by temperature)
    return cs


def truth_rank(contracts, actual_f):
    """Rank of the truth bucket among contracts ordered by ask desc (0 = modal)."""
    ti = None
    for j, (spec, _) in enumerate(contracts):
        pred, _p = parse_bucket(spec)
        if pred(actual_f):
            ti = j
            break
    if ti is None:
        return None
    asks = [a for _, a in contracts]
    order = sorted(range(len(contracts)), key=lambda j: -asks[j])
    return order.index(ti)


def main():
    kalshi = pd.read_parquet(_ROOT / "data/kalshi_history.parquet")
    kalshi["event_date"] = pd.to_datetime(kalshi["event_date"]).dt.normalize()
    kalshi["timestamp"] = pd.to_datetime(kalshi["timestamp"], utc=True)
    cli = pd.read_parquet(_ROOT / "data/cli_KMDW.parquet")
    cli["date"] = pd.to_datetime(cli["date"]).dt.normalize()
    truth = {d: int(t) for d, t in
             cli[["date", "max_temp_f"]].dropna().itertuples(index=False, name=None)}

    rows, only_1pm = [], 0
    for ev in sorted(kalshi["event_date"].unique()):
        ev = pd.Timestamp(ev).normalize()
        if ev not in truth:
            continue
        c_mid, c_1pm = snapshot(kalshi, ev, 0), snapshot(kalshi, ev, 13)
        if c_1pm is None:
            continue
        r_1pm = truth_rank(c_1pm, truth[ev])
        if r_1pm is None:
            continue
        if c_mid is None:
            only_1pm += 1
            continue
        r_mid = truth_rank(c_mid, truth[ev])
        if r_mid is None:
            only_1pm += 1
            continue
        rows.append((r_mid, r_1pm))

    df = pd.DataFrame(rows, columns=["mid", "pm"])
    n = len(df)
    print("=" * 52)
    print("MARKET MODAL COVERAGE — midnight vs 1 PM snapshot")
    print("=" * 52)
    print(f"Common days (valid book at both anchors + truth): n={n}")
    if only_1pm:
        print(f"({only_1pm} extra days had a 1 PM book but no usable midnight book — excluded)")
    print()
    print(f"  {'cumulative coverage':<22}{'midnight':>10}{'1 PM':>9}{'Δ':>8}")
    for k, name in [(1, "1st modal"), (2, "1st+2nd modal"), (3, "1st+2nd+3rd modal")]:
        cm, cp = (df["mid"] < k).mean(), (df["pm"] < k).mean()
        print(f"  {name:<22}{cm:>10.1%}{cp:>9.1%}{cp - cm:>+8.1%}")
    # marginal (exactly the k-th modal) for context
    print()
    print(f"  {'marginal (exactly Nth)':<22}{'midnight':>10}{'1 PM':>9}")
    for k, name in [(1, "1st modal"), (2, "2nd modal"), (3, "3rd modal")]:
        cm = ((df["mid"] == k - 1)).mean()
        cp = ((df["pm"] == k - 1)).mean()
        print(f"  {name:<22}{cm:>10.1%}{cp:>9.1%}")


if __name__ == "__main__":
    main()
