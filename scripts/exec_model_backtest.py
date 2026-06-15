"""Execution-model comparison: per-city-local anchor (LIVE model) vs single-UTC-time (batch model).

Same selection rule for BOTH (cross-city top-3 by confidence, band 0.93-0.95, 50% split, $250,
compound) so the ONLY difference is WHEN each city's favorite price is read:
  Method A (LIVE / per-anchor): each city at its OWN 17:00 (high) / 22:00 (low) LOCAL anchor.
  Method B (single-UTC):       every city at one fixed UTC instant T (swept).

At a single T, early-tz cities have usually converged out of 0.93-0.95 (favorite -> ~1.0) and west-tz
cities haven't reached the band yet, so B only "catches" the tz whose anchor ~ T. The sweep shows it.

Last-trade prices from the deep backfill (high: recent 2026; low: ~67d). Truth = settlement_value.

Usage: python scripts/exec_model_backtest.py [--rebuild]
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
from zoneinfo import ZoneInfo
import numpy as np, pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from scripts.late_night_coverage import HIGH, LOW, load_series, find_base

CACHE = _ROOT / "data" / "exec_model_events.parquet"
BAND = (0.93, 0.95); NC = 3; STAKE = 0.50; BR = 250.0
# Method-B single-UTC instants, (day_offset_from_event_date, hour_UTC):
UTC_T = {
    "high": {"T21": (0, 21), "T22": (0, 22), "T23": (0, 23), "T00": (1, 0)},   # 17:00 ET..PT span
    "low":  {"T02": (1, 2), "T03": (1, 3), "T04": (1, 4), "T05": (1, 5), "T06": (1, 6)},  # 22:00 ET..PT
}
ANCHOR_HM = {"high": (17, 0), "low": (22, 0)}
SINCE = {"high": "2026-01-01", "low": None}


def build(base: Path) -> pd.DataFrame:
    rows = []
    for market, cmap in (("high", HIGH), ("low", LOW)):
        hh, mm = ANCHOR_HM[market]
        for series, (label, tz_name) in cmap.items():
            if not (base / series).is_dir():
                continue
            df = load_series(base, series, SINCE[market])
            if df.empty:
                continue
            tz = ZoneInfo(tz_name)
            for ev, day in df.groupby("event_date"):
                w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
                if len(w) != 1:
                    continue
                win = w[0]
                d = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
                if d.empty:
                    continue
                evals = {"anchor": pd.Timestamp(ev.year, ev.month, ev.day, hh, mm, tz=tz).tz_convert("UTC")}
                mid = pd.Timestamp(ev.year, ev.month, ev.day, tz="UTC")
                for lbl, (doff, hr) in UTC_T[market].items():
                    evals[lbl] = mid + pd.Timedelta(days=doff, hours=hr)
                for lbl, t in evals.items():
                    pre = d[d["timestamp"] <= t]
                    if pre.empty:
                        continue
                    last = pre.groupby("ticker")["yes_price_cents"].last()
                    if last.size < 3:
                        continue
                    fav = last.idxmax()
                    rows.append({"market": market, "date": ev, "series": series, "city": label,
                                 "eval": lbl, "price": float(last[fav]) / 100.0, "win": bool(fav == win)})
            print(f"  built {market} {series}", flush=True)
    out = pd.DataFrame(rows); out.to_parquet(CACHE, index=False); return out


def backtest(df_eval: pd.DataFrame):
    """Same rule: per date, cities with price in band -> top-NC by price -> 50% split, compound."""
    bal = BR; eq = [bal]; daily = []; pk = wn = am = 0; days = 0
    for d, day in df_eval.sort_values("date").groupby("date"):
        cand = day[(day.price >= BAND[0]) & (day.price <= BAND[1])].sort_values("price", ascending=False)
        if cand.empty:
            continue
        days += 1
        chosen = cand.head(NC)
        if not chosen["win"].any():
            am += 1
        per = STAKE / len(chosen)
        pay = 0.0
        for c in chosen.itertuples():
            pay += per * bal / (c.price * (1 + 0.07 * (1 - c.price))) if c.win else 0.0
            pk += 1; wn += int(c.win)
        bal = bal - STAKE * bal + pay; daily.append(bal - eq[-1]); eq.append(bal)
        if bal < 1e-9:
            break
    eq = np.array(eq); daily = np.array(daily); peak = np.maximum.accumulate(eq)
    return dict(days=days, picks=pk, hit=wn / pk if pk else float("nan"), final=bal, pnl=bal - BR,
                dd=float((eq / peak - 1).min()) if len(eq) > 1 else 0.0,
                worst=daily.min() if len(daily) else 0.0, allmiss=am,
                avgpk=pk / days if days else 0.0)


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--rebuild", action="store_true"); a = ap.parse_args(argv)
    if CACHE.exists() and not a.rebuild:
        big = pd.read_parquet(CACHE); print(f"loaded cache {CACHE} ({len(big)} rows)")
    else:
        print("building eval table from backfill (high=2026, low=~67d)..."); big = build(find_base())
    big["date"] = pd.to_datetime(big["date"])
    for market in ("high", "low"):
        m = big[big.market == market]
        print(f"\n{'='*78}\n{market.upper()}-TEMP | same rule (top-3, band 0.93-0.95, 50% split, $250) | "
              f"A=per-city-local-anchor vs B=single-UTC-T\n{'='*78}")
        print(f"{'method':>22}{'days':>6}{'picks':>7}{'avgPk':>6}{'hit':>7}{'$final':>9}{'PnL':>8}{'maxDD':>7}{'worst':>7}{'allmiss':>8}")
        order = ["anchor"] + list(UTC_T[market])
        for lbl in order:
            sub = m[m["eval"] == lbl]
            if sub.empty:
                continue
            r = backtest(sub)
            tag = "A: LOCAL anchor" if lbl == "anchor" else f"B: all @ {lbl[1:]}:00 UTC"
            fin = "RUIN" if r["final"] < BR * 0.05 else f"{r['final']:.0f}"
            print(f"{tag:>22}{r['days']:>6}{r['picks']:>7}{r['avgpk']:>6.1f}{r['hit']:>7.1%}{fin:>9}"
                  f"{r['pnl']:>+8.0f}{r['dd']:>7.0%}{r['worst']:>+7.0f}{r['allmiss']:>5}/{r['days']:<3}")
    print("\nA reads each city at its OWN local 17:00/22:00 (what a per-anchor live bot gets);")
    print("B reads ALL cities at one UTC instant (what a once-daily-UTC bot gets) -- see how few/which")
    print("cities are in-band at each T, and how the timing changes hit/PnL vs the per-anchor model.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
