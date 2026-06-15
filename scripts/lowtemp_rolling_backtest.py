"""Rolling-entry low-temp backtest (user spec).

ENTRY: starting at 11 PM local, step every 15 min (11:00, 11:15, 11:30, 11:45, 12:00). For each
city's low-temp event, the FIRST snapshot at which the favorite (largest modal bucket) price lands
in the [85%, 95%] band is the entry — buy the favorite then. Cities in-band at 11:00 enter at 11:00;
cities still <85% wait and enter when they rise into the band; cities already >95% (or that jump past
the band, or never reach it) are never bought. One entry per city per day.

SIZING (user spec): each event-day, split the account balance evenly across ALL AVAILABLE low-temp
markets that day (e.g. 9 open markets -> balance/9 per city). Deploy that slice ONLY for cities that
entered the band (buy the favorite); the slices for non-qualifying available cities stay as CASH and
carry forward. So at-risk capital = (entered / available) of the balance — never 100%. Compound.

Entry price = favorite LAST TRADE <= entry snapshot (no low-temp ask exists -> OPTIMISTIC).
"Available" = a city whose low-temp market has >=3 priced buckets at the 11 PM scan (i.e. tradeable
when you look). Truth = Kalshi settlement_value. Window = ~62 traded days x 19 cities (Apr-Jun 2026).

Usage:
  python scripts/lowtemp_rolling_backtest.py                       # band 85-95%, 11PM->12AM /15min
  python scripts/lowtemp_rolling_backtest.py --band-lo 0.85 --band-hi 0.95 --bankroll 25
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
from scripts.late_night_coverage import LOW, load_series, find_base

ROLL = [(23, 0), (23, 15), (23, 30), (23, 45), (24, 0)]   # 11:00 .. 12:00 local, 15-min steps


def lbl(hh, mm):
    return "00:00" if hh == 24 else f"{hh:02d}:{mm:02d}"


def _contracts(alloc, p):
    return alloc / (p * (1.0 + 0.07 * (1.0 - p))) if alloc > 0 else 0.0


def _fav_at(d: pd.DataFrame, local_utc) -> tuple:
    """(favorite ticker, price) using last trade <= snapshot; (None, None) if <3 buckets priced."""
    pre = d[d["timestamp"] <= local_utc]
    if pre.empty:
        return None, None
    last = pre.groupby("ticker")["yes_price_cents"].last()
    if last.size < 3:
        return None, None
    fav = last.sort_values(ascending=False).index[0]
    return fav, float(last[fav]) / 100.0


def rolling_events(base: Path, series: str, tz_name: str, lo: float, hi: float) -> list[dict]:
    """One row per AVAILABLE event (tradeable at 11 PM). entered=True iff its favorite first hits
    the band during the 11PM->12AM roll; then price/win/entry are filled."""
    df = load_series(base, series)
    if df.empty:
        return []
    tz = ZoneInfo(tz_name)
    out = []
    for ev, day in df.groupby("event_date"):
        win = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(win) != 1:
            continue
        win = win[0]
        d = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        if d.empty:
            continue
        scan0 = pd.Timestamp(ev.year, ev.month, ev.day, 23, 0, tz=tz).tz_convert("UTC")
        fav0, _ = _fav_at(d, scan0)
        if fav0 is None:
            continue                                   # not tradeable at the 11 PM scan -> not "available"
        row = {"date": ev, "series": series, "entered": False,
               "price": np.nan, "win": False, "entry": ""}
        for hh, mm in ROLL:                            # walk snapshots in time order; first in-band wins
            local = (pd.Timestamp(ev.year, ev.month, ev.day, 0, 0, tz=tz) + pd.Timedelta(days=1)
                     if hh == 24 else pd.Timestamp(ev.year, ev.month, ev.day, hh, mm, tz=tz))
            fav, p = _fav_at(d, local.tz_convert("UTC"))
            if fav is None:
                continue
            if lo <= p <= hi:
                row.update(entered=True, price=p, win=bool(fav == win), entry=lbl(hh, mm))
                break
        out.append(row)
    return out


def simulate(big: pd.DataFrame, bankroll: float):
    """balance/available per city/day; deploy on entered, cash on the rest; compound."""
    bal = bankroll
    curve = [bal]
    wipeouts = 0
    deploy_fr = []
    for _, day in big.groupby("date"):
        navail = len(day)                              # all rows are available that day
        alloc = bal / navail
        ent = day[day["entered"]]
        deploy_fr.append(len(ent) / navail)
        win_payoff = sum(_contracts(alloc, e.price) for e in ent.itertuples() if e.win)
        cash = (navail - len(ent)) * alloc             # non-entered available slices stay cash
        new_bal = win_payoff + cash
        if bal > 0 and new_bal / bal < 0.5:
            wipeouts += 1
        bal = new_bal
        curve.append(bal)
    curve = np.array(curve)
    maxdd = float((curve / np.maximum.accumulate(curve) - 1.0).min())
    ent = big[big["entered"]]
    pe = np.where(ent["win"].values, 1.0 / ent["price"].values - 1.0, -1.0)
    net = pe.mean() - 0.07 * (1.0 - ent["price"]).mean()
    se = pe.std() / np.sqrt(len(pe)) if len(pe) else float("nan")
    return {"available": len(big), "entered": int(big["entered"].sum()),
            "deploy_frac": float(np.mean(deploy_fr)), "winrate": ent["win"].mean(),
            "avgpx": ent["price"].mean(), "netEV": net, "z": net / se if se else float("nan"),
            "final": bal, "ret": bal / bankroll - 1, "wipeouts": wipeouts, "maxdd": maxdd,
            "ruin": bal < 1e-6}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=25.0)
    ap.add_argument("--band-lo", type=float, default=0.85)
    ap.add_argument("--band-hi", type=float, default=0.95)
    ap.add_argument("--out", default="data/lowtemp_rolling_8595.parquet")
    args = ap.parse_args(argv)
    base = find_base()
    print(f"base={base} | $ {args.bankroll:.0f} | band {args.band_lo:.0%}-{args.band_hi:.0%} | "
          f"rolling {[lbl(h,m) for h,m in ROLL]} (first in-band) | size=balance/AVAILABLE-mkts")

    rows = []
    for s, (label, tz) in LOW.items():
        if (base / s).is_dir():
            rows.extend(rolling_events(base, s, tz, args.band_lo, args.band_hi))
    if not rows:
        print("no events")
        return 1
    big = pd.DataFrame(rows)
    big["city"] = big["series"].map(lambda s: LOW[s][0])
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    big.to_parquet(args.out, index=False)

    r = simulate(big, args.bankroll)
    print("\n" + "=" * 80)
    print("ROLLING ENTRY (first favorite in 85-95% band, 11PM->12AM) — $25 split / available markets")
    print("=" * 80)
    print(f"  available city-days : {r['available']}   entered (deployed): {r['entered']}  "
          f"({r['entered']/r['available']:.0%} of available)")
    print(f"  avg nightly deploy  : {r['deploy_frac']:.0%} of balance at risk (rest cash)")
    print(f"  win rate (entered)  : {r['winrate']:.1%}   avg entry px: {r['avgpx']:.3f}")
    print(f"  netEV per event     : {r['netEV']:+.1%}   z={r['z']:.2f}  (last-trade; real ask -> worse)")
    final_str = "RUIN" if r["ruin"] else f"${r['final']:.2f} ({r['ret']:+.0%})"
    print(f"  $25 final           : {final_str}   maxDD={r['maxdd']:.0%}   wipeouts={r['wipeouts']}")

    vc = big[big["entered"]]["entry"].value_counts().reindex([lbl(h, m) for h, m in ROLL]).fillna(0).astype(int)
    print("\n  when entered cities first hit the band:")
    for k, v in vc.items():
        print(f"    {k}: {v:4d}  ({v/max(1,r['entered']):.0%})")

    print("\n  per-city (entered only):")
    print(f"  {'city':<14}{'avail':>6}{'entered':>8}{'winrate':>8}{'avgpx':>7}{'netEV':>8}")
    pc = []
    for s, g in big.groupby("series"):
        e = g[g["entered"]]
        if e.empty:
            continue
        p = e["price"].values; w = e["win"].values.astype(bool)
        net = np.where(w, 1.0 / p - 1.0, -1.0).mean() - 0.07 * (1.0 - p).mean()
        pc.append((LOW[s][0], len(g), len(e), w.mean(), p.mean(), net))
    for city, av, en, wr, px, net in sorted(pc, key=lambda x: -x[5]):
        print(f"  {city:<14}{av:>6}{en:>8}{wr:>8.0%}{px:>7.2f}{net:>+8.0%}")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
