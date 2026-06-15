"""High-temp 4-5 PM modal backtest — buy the favorite in a price band, $25 account.

Regime: the daily HIGH locks in mid-afternoon, so 4-5 PM is where its market actually converges
(unlike 8 PM-midnight, where high-temp is already pinned ~1.00). Entry stamps: 16:00, 16:15, 16:30,
16:45, 17:00 LOCAL per city. For each fixed stamp, buy the favorite (largest modal bucket) when its
price is in [band_lo, band_hi]; split the account across the cities TRADED that day, capped so no
single position exceeds `cap` of the balance (the 50% rule); rest is cash; compound to settlement.

Entry price = favorite LAST TRADE <= stamp (deep backfill; ask cross-check is a separate step using
the 13-day orderbook tape). Truth = Kalshi settlement_value. Reported for full 3.4 yr AND the current
regime (--since), because the high-temp edge decayed across years.

Usage:
  python scripts/hightemp_4pm_backtest.py                      # band 90-95%, cap 50%
  python scripts/hightemp_4pm_backtest.py --band-lo 0.90 --band-hi 0.95 --cap 0.50 --since 2026-04-01
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from scripts.late_night_coverage import HIGH, load_series, find_base

SNAPS = [(16, 0), (16, 15), (16, 30), (16, 45), (17, 0)]   # 4:00 .. 5:00 PM local, 15-min
CACHE = _ROOT / "data" / "hightemp_4pm_events.parquet"


def lbl(hh, mm):
    return f"{hh:02d}:{mm:02d}"


def build_events(base: Path) -> pd.DataFrame:
    """Per (series, event_date, snap): favorite last-trade price + win. Cached (heavy: 3.4yr)."""
    rows = []
    for series, (label, tz_name) in HIGH.items():
        if not (base / series).is_dir():
            continue
        df = load_series(base, series)
        if df.empty:
            continue
        tz = ZoneInfo(tz_name)
        for ev, day in df.groupby("event_date"):
            win = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
            if len(win) != 1:
                continue
            win = win[0]
            d = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
            if d.empty:
                continue
            for hh, mm in SNAPS:
                snap_utc = pd.Timestamp(ev.year, ev.month, ev.day, hh, mm, tz=tz).tz_convert("UTC")
                pre = d[d["timestamp"] <= snap_utc]
                if pre.empty:
                    continue
                last = pre.groupby("ticker")["yes_price_cents"].last()
                if last.size < 3:
                    continue
                fav = last.sort_values(ascending=False).index[0]
                rows.append({"series": series, "city": label, "date": ev, "snap": lbl(hh, mm),
                             "price": float(last[fav]) / 100.0, "win": bool(fav == win)})
        print(f"  built {series} ({label})", flush=True)
    out = pd.DataFrame(rows)
    out.to_parquet(CACHE, index=False)
    return out


def _contracts(a, p):
    return a / (p * (1.0 + 0.07 * (1.0 - p))) if a > 0 else 0.0


def backtest(rows: pd.DataFrame, lo: float, hi: float, cap: float, bankroll: float = 25.0):
    """Fixed-stamp: split across TRADED cities, per-position cap, compound. rows = one snap."""
    q = rows[(rows.price >= lo) & (rows.price <= hi)]
    if q.empty:
        return None
    bal = bankroll
    curve = [bal]
    wipe = 0
    for _, day in rows.groupby("date"):           # iterate all available days; trade the qualifiers
        tr = day[(day.price >= lo) & (day.price <= hi)]
        K = len(tr)
        if K == 0:
            curve.append(bal)
            continue
        a = min(bal / K, cap * bal)
        deployed = K * a
        winp = sum(_contracts(a, e.price) for e in tr.itertuples() if e.win)
        nb = winp + (bal - deployed)
        if bal > 0 and nb / bal < 0.5:
            wipe += 1
        bal = nb
        curve.append(bal)
    curve = np.array(curve)
    maxdd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    pe = np.where(q.win.values, 1.0 / q.price.values - 1.0, -1.0)
    net = pe.mean() - 0.07 * (1.0 - q.price).mean()
    se = pe.std() / np.sqrt(len(pe))
    return {"days": q.date.nunique(), "events": len(q), "winrate": q.win.mean(),
            "avgpx": q.price.mean(), "netEV": net, "z": net / se if se else float("nan"),
            "final": bal, "ret": bal / bankroll - 1, "maxdd": maxdd, "wipe": wipe, "ruin": bal < 1e-6}


def report(rows: pd.DataFrame, lo, hi, cap, title):
    print("\n" + "=" * 96)
    print(f"HIGH-TEMP 4-5PM | {title} | band {lo:.0%}-{hi:.0%} | split across traded, cap {cap:.0%}/pos | $25")
    print("=" * 96)
    print(f"{'stamp':>6}{'days':>6}{'events':>7}{'winrate':>8}{'avgpx':>7}{'netEV':>8}{'z':>6}{'$final':>10}{'ret%':>9}{'maxDD':>7}{'wipe':>5}")
    best = None
    for hh, mm in SNAPS:
        r = backtest(rows[rows.snap == lbl(hh, mm)], lo, hi, cap)
        if not r:
            continue
        if best is None or r["final"] > best[1]["final"]:
            best = (lbl(hh, mm), r)
        fin = "RUIN" if r["ruin"] else f"{r['final']:.2f}"
        print(f"{lbl(hh,mm):>6}{r['days']:>6}{r['events']:>7}{r['winrate']:>8.1%}{r['avgpx']:>7.2f}"
              f"{r['netEV']:>+8.1%}{r['z']:>6.2f}{fin:>10}{r['ret']:>+9.0%}{r['maxdd']:>7.0%}{r['wipe']:>5}")
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--band-lo", type=float, default=0.90)
    ap.add_argument("--band-hi", type=float, default=0.95)
    ap.add_argument("--cap", type=float, default=0.50)
    ap.add_argument("--since", default="2026-04-01")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)

    if CACHE.exists() and not args.rebuild:
        big = pd.read_parquet(CACHE)
        print(f"loaded cache {CACHE} ({len(big)} rows)")
    else:
        print("building per-event table from 3.4yr high backfill (heavy)...")
        big = build_events(find_base())
    big["date"] = pd.to_datetime(big["date"])

    report(big, args.band_lo, args.band_hi, args.cap, "FULL 3.4yr")
    recent = big[big["date"] >= pd.Timestamp(args.since)]
    best = report(recent, args.band_lo, args.band_hi, args.cap, f"RECENT (since {args.since})")

    if best:
        stamp, _ = best
        print(f"\nper-city @ {stamp} (RECENT, band {args.band_lo:.0%}-{args.band_hi:.0%}):")
        sub = recent[(recent.snap == stamp) & (recent.price >= args.band_lo) & (recent.price <= args.band_hi)]
        print(f"  {'city':<14}{'N':>4}{'winrate':>8}{'avgpx':>7}{'netEV':>8}")
        pc = []
        for c, g in sub.groupby("city"):
            p = g.price.values; w = g.win.values.astype(bool)
            pc.append((c, len(g), w.mean(), p.mean(), np.where(w, 1/p-1, -1).mean()-0.07*(1-p).mean()))
        for c, n, wr, px, nv in sorted(pc, key=lambda x: -x[4]):
            print(f"  {c:<14}{n:>4}{wr:>8.0%}{px:>7.2f}{nv:>+8.0%}")
    print("\nNOTE: last-trade entry (high-temp HAS a 13-day orderbook tape for a real-ask check next).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
