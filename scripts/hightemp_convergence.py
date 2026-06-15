"""Fundamental variant: BUY-AND-EXIT convergence trade on the high-temp afternoon favorite.

Core kept intact: buy the favorite (largest modal bucket) of each high-temp city in the afternoon.
Changed fundamentally: instead of HOLDING to 0/1 settlement (payoff asymmetry: win ~+6c, lose ~-94c),
buy the favorite at an ENTRY stamp and SELL the SAME bucket at a later EXIT stamp at its then-price.
On the ~5% of days the favorite is wrong, its price collapses by the exit, so you sell at a managed
loss (e.g. -40c) instead of -100c — converting the binary tail into a continuous P&L.

Builds, per (city, event_date, entry stamp): the entry favorite's ticker + its LAST-TRADE price at
entry and at each later exit stamp (carry-forward), and the 0/1 settle outcome. Then compares, OOS
(train<2026 / test 2026), every (entry, exit) pair vs hold-to-settle: mean return per $ deployed,
z, % of trades profitable, drawdown of the per-event return stream. Optional band filter at entry.

Return per $ deployed (sell): (p_exit - p_entry - buy_fee - sell_fee) / p_entry.
Return per $ deployed (hold): (win - p_entry - buy_fee) / p_entry.

Usage: python scripts/hightemp_convergence.py [--rebuild] [--band-lo 0 --band-hi 1]
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

ENTRY = [(14, 0), (15, 0), (16, 0)]
EXIT = [(16, 0), (17, 0), (18, 0), (20, 0), (23, 0)]   # + settle
CACHE = _ROOT / "data" / "hightemp_convergence.parquet"
SPLIT = "2026-01-01"
FEE = lambda p: 0.07 * p * (1.0 - p)


def lbl(hh, mm):
    return f"{hh:02d}:{mm:02d}"


def price_at(d, tkr, snap_utc):
    pre = d[(d.ticker == tkr) & (d.timestamp <= snap_utc)]
    return float(pre["yes_price_cents"].iloc[-1]) / 100.0 if len(pre) else np.nan


def build(base: Path) -> pd.DataFrame:
    rows = []
    for series, (label, tz_name) in HIGH.items():
        if not (base / series).is_dir():
            continue
        df = load_series(base, series)
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
            for eh, em in ENTRY:
                esnap = pd.Timestamp(ev.year, ev.month, ev.day, eh, em, tz=tz).tz_convert("UTC")
                pre = d[d["timestamp"] <= esnap]
                if pre.empty:
                    continue
                last = pre.groupby("ticker")["yes_price_cents"].last().sort_values(ascending=False)
                if last.size < 3:
                    continue
                fav = last.index[0]
                rec = {"series": series, "city": label, "date": ev, "entry": lbl(eh, em),
                       "p_entry": last.iloc[0] / 100.0, "win": bool(fav == win)}
                for xh, xm in EXIT:
                    if (xh, xm) < (eh, em):
                        rec[lbl(xh, xm)] = np.nan
                        continue
                    xsnap = pd.Timestamp(ev.year, ev.month, ev.day, xh, xm, tz=tz).tz_convert("UTC")
                    rec[lbl(xh, xm)] = price_at(d, fav, xsnap)
                rows.append(rec)
        print(f"  built {series} ({label})", flush=True)
    out = pd.DataFrame(rows)
    out.to_parquet(CACHE, index=False)
    return out


def stats(ret):
    ret = ret[~np.isnan(ret)]
    if len(ret) < 5:
        return np.nan, np.nan, 0, np.nan
    se = ret.std() / np.sqrt(len(ret))
    return ret.mean(), (ret.mean() / se if se else np.nan), len(ret), float((ret > 0).mean())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--band-lo", type=float, default=0.0)
    ap.add_argument("--band-hi", type=float, default=1.0)
    args = ap.parse_args(argv)
    if CACHE.exists() and not args.rebuild:
        big = pd.read_parquet(CACHE); print(f"loaded {CACHE} ({len(big)} rows)")
    else:
        print("building convergence path table (heavy)..."); big = build(find_base())
    big["date"] = pd.to_datetime(big["date"])
    big = big[(big.p_entry >= args.band_lo) & (big.p_entry <= args.band_hi)]
    print(f"band {args.band_lo:.0%}-{args.band_hi:.0%} | rows={len(big)}\n")

    print("=== (entry -> exit) convergence vs hold, OOS [train<2026 | test 2026] ===")
    print(f"{'entry':>6}{'exit':>8}{'  test mean/$':>14}{'z':>6}{'%win':>6}{'N':>6}{'  train z':>9}")
    res = []
    for eh, em in ENTRY:
        e = lbl(eh, em)
        sub = big[big.entry == e]
        for xh, xm in EXIT + [(99, 0)]:
            x = "settle" if xh == 99 else lbl(xh, xm)
            if xh != 99 and (xh, xm) < (eh, em):
                continue
            def ret_of(df):
                pe = df.p_entry.values
                if x == "settle":
                    return (df.win.values.astype(float) - pe - FEE(pe)) / pe
                px = df[x].values
                return (px - pe - FEE(pe) - FEE(px)) / pe
            tr, te = big[(big.entry == e) & (big.date < SPLIT)], big[(big.entry == e) & (big.date >= SPLIT)]
            tr, te = tr[tr.entry == e], te[te.entry == e]
            # filter rows with valid exit price
            rtr = ret_of(tr); rte = ret_of(te)
            mtr, ztr, _, _ = stats(rtr); mte, zte, Nte, wte = stats(rte)
            if np.isnan(zte):
                continue
            res.append((e, x, mte, zte, wte, Nte, ztr))
    res.sort(key=lambda r: -(min(r[3], r[6]) if not np.isnan(r[6]) else r[3]))
    for e, x, mte, zte, wte, Nte, ztr in res[:20]:
        sig = "*" if (zte > 1.64 and (not np.isnan(ztr) and ztr > 1.64)) else " "
        print(f"{sig}{e:>5}{x:>8}{mte:>+13.1%}{zte:>6.1f}{wte:>6.0%}{int(Nte):>6}{ztr:>9.1f}")
    print("\n* = significant (z>1.64) in BOTH train and test. mean/$ = mean return per $ deployed.")
    print("Compare 'settle' rows (hold to 0/1) vs earlier-exit rows (sell the bucket) for the same entry.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
