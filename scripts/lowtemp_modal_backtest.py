"""Backtest: best LOCAL HOUR to buy the favorite on LOW-temp markets, $25 account.

Strategy (user spec):
  At a fixed local entry hour h (sweep 22:00, 22:30, 23:00, 23:30, 00:00), for every city's
  low-temp event that day, look at the LARGEST MODAL bucket (the favorite = highest-priced bucket).
  If its implied prob (price) is <= THRESH (default 0.90) it qualifies. BUY the favorite of every
  qualifying event, splitting the WHOLE account balance EVENLY across all qualifying events that
  day. Hold to settlement; favorite pays $1 if it settled 'yes', else $0. Compound day to day.

Truth = Kalshi settlement_value. Entry price = favorite's LAST TRADE <= h (the only low-temp price
we have — no orderbook ask exists for low-temp, so the real fill ask is HIGHER and these results are
an OPTIMISTIC upper bound). Fees = canonical Kalshi taker fee, applied as fraction 0.07*(1-P) of
notional (fee_cents = ceil(7%*N*P*(1-P)) => fee/cost = 7%*(1-P)).

Usage:
  python scripts/lowtemp_modal_backtest.py                 # $25, thresh 0.90, compounding
  python scripts/lowtemp_modal_backtest.py --bankroll 25 --thresh 0.90
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
from scripts.late_night_coverage import LOW, load_series, find_base, snap_label

SNAPS_LATE = [(22, 0), (22, 30), (23, 0), (23, 30), (24, 0)]   # 10PM .. midnight (event-day end)


def event_rows(base: Path, series: str, tz_name: str) -> list[dict]:
    """Per (event_date, late snapshot): favorite price (last trade) + did it win."""
    df = load_series(base, series)
    if df.empty:
        return []
    tz = ZoneInfo(tz_name)
    out = []
    for ev_date, day in df.groupby("event_date"):
        win = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(win) != 1:
            continue
        win = win[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        if day.empty:
            continue
        for hh, mm in SNAPS_LATE:
            if hh == 24:
                local = pd.Timestamp(ev_date.year, ev_date.month, ev_date.day, 0, 0, tz=tz) + pd.Timedelta(days=1)
            else:
                local = pd.Timestamp(ev_date.year, ev_date.month, ev_date.day, hh, mm, tz=tz)
            pre = day[day["timestamp"] <= local.tz_convert("UTC")]
            if pre.empty:
                continue
            last = pre.groupby("ticker")["yes_price_cents"].last()
            if last.size < 3:
                continue
            fav = last.sort_values(ascending=False).index[0]
            out.append({
                "snap": snap_label(hh, mm), "date": ev_date, "series": series,
                "price": float(last[fav]) / 100.0, "win": fav == win,
            })
    return out


def _contracts(alloc: float, p: float) -> float:
    return alloc / (p * (1.0 + 0.07 * (1.0 - p))) if alloc > 0 else 0.0   # fee-adjusted


def simulate(g: pd.DataFrame, bankroll: float, band_lo: float, band_hi: float, frac: float = 0.25):
    """At one entry hour: the literal spec (deploy 100% of balance, split evenly, compound) AND a
    de-risked variant (deploy only `frac` of balance/day, rest cash). g = rows for that hour.
    Qualify = favorite price in [band_lo, band_hi]. Ruin is absorbing but NOT early-broken."""
    q = g[(g["price"] >= band_lo) & (g["price"] <= band_hi)].copy()
    if q.empty:
        return None
    bal = bankroll          # full-deployment (spec)
    balf = bankroll         # fractional-deployment (robustness)
    curve = [bal]
    per_event_ret, n_events, wins, wipeouts = [], 0, 0, 0
    for _, day in q.groupby("date"):
        K = len(day)
        alloc, allocf = bal / K, (frac * balf) / K
        new_bal, win_payoff_f = 0.0, 0.0
        for _, e in day.iterrows():
            p = e["price"]
            new_bal += _contracts(alloc, p) if e["win"] else 0.0
            win_payoff_f += _contracts(allocf, p) if e["win"] else 0.0
            per_event_ret.append((1.0 / p - 1.0) if e["win"] else -1.0)
            n_events += 1
            wins += int(e["win"])
        if bal > 0 and new_bal / bal < 0.5:
            wipeouts += 1
        bal = new_bal
        balf = balf - frac * balf + win_payoff_f     # cash kept + winnings
        curve.append(bal)
    curve = np.array(curve)
    peak = np.maximum.accumulate(curve)
    maxdd = float((curve / peak - 1.0).min()) if len(curve) > 1 else 0.0
    pe = np.array(per_event_ret)
    feerate = 0.07 * (1.0 - q["price"]).mean()
    return {
        "final": bal, "ret_pct": bal / bankroll - 1.0, "final_frac": balf,
        "ret_frac": balf / bankroll - 1.0, "trade_days": curve.size - 1, "wipeouts": wipeouts,
        "events": n_events, "winrate": wins / n_events if n_events else float("nan"),
        "mean_price": q["price"].mean(), "gross_ev": pe.mean(),
        "net_ev": pe.mean() - feerate, "maxdd": maxdd, "ruin": bal < 1e-6,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bankroll", type=float, default=25.0)
    ap.add_argument("--band-lo", type=float, default=0.0, help="lower bound on favorite price to qualify")
    ap.add_argument("--thresh", type=float, default=0.90, help="upper bound on favorite price to qualify")
    ap.add_argument("--base", default=None)
    ap.add_argument("--out", default="data/lowtemp_modal_backtest.parquet")
    args = ap.parse_args(argv)
    base = Path(args.base) if args.base else find_base()
    print(f"base={base} | bankroll=${args.bankroll:.0f} | qualify band {args.band_lo:.0%}<=px<={args.thresh:.0%} | "
          f"entry hours={[snap_label(h, m) for h, m in SNAPS_LATE]}")

    rows = []
    for series, (label, tz) in LOW.items():
        if not (base / series).is_dir():
            continue
        rows.extend(event_rows(base, series, tz))
    if not rows:
        print("no low-temp rows — backfill missing?")
        return 1
    big = pd.DataFrame(rows)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    big.to_parquet(args.out, index=False)

    order = {snap_label(h, m): i for i, (h, m) in enumerate(SNAPS_LATE)}
    res = []
    for snap, g in big.groupby("snap"):
        r = simulate(g, args.bankroll, args.band_lo, args.thresh)
        if r:
            r["snap"] = snap
            res.append(r)
    res.sort(key=lambda r: order[r["snap"]])

    print("\n" + "=" * 104)
    print(f"BEST LOCAL ENTRY HOUR — low-temp 'buy favorite if {args.band_lo:.0%}<=modal<={args.thresh:.0%}', $25 split evenly/day")
    print("=" * 104)
    print(f"{'entry':>6}{'days':>6}{'events':>7}{'winrate':>8}{'avgPx':>7}{'netEV':>8}"
          f"{'full$':>9}{'fullret%':>9}{'wipeout':>8}{'frac25$':>9}{'fracret%':>9}{'maxDD':>7}")
    # rank by the de-risked (non-ruinous) growth — the only economically meaningful ordering
    best = max(res, key=lambda r: r["final_frac"])
    for r in res:
        star = " *best" if r is best else ""
        full = "RUIN" if r["ruin"] else f"{r['final']:.2f}"
        print(f"{r['snap']:>6}{r['trade_days']:>6}{r['events']:>7}{r['winrate']:>8.1%}{r['mean_price']:>7.2f}"
              f"{r['net_ev']:>+8.1%}{full:>9}{r['ret_pct']:>+9.1%}{r['wipeouts']:>8}"
              f"{r['final_frac']:>9.2f}{r['ret_frac']:>+9.1%}{r['maxdd']:>7.0%}{star}")
    print("\nfull$  = literal spec: 100% of balance split evenly across qualifying events, compounding.")
    print("frac25$= de-risked: only 25% of balance deployed/day (rest cash) — isolates edge from ruin.")
    print("wipeout= # days the account fell >50% in one night (correlated low-temp losses across cities).")
    print("netEV  = sizing-free per-event EV (last trade, fee-adj). Entry px = favorite LAST TRADE;")
    print("         low-temp has NO orderbook ask, so real fills are WORSE -> netEV is an upper bound.")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
