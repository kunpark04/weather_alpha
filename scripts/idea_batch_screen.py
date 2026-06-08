"""idea_batch_screen.py -- batch #1 of model-free edge screens on the deep tape (strat_prod_2 repoint).

MODEL-FREE only: market data (last-trade price, settlement, calendar, bucket structure). No v3 model,
no weather as a predictor. All screens split IS/OOS 80/20 by date. Screens NOT already covered by the
~60 existing scripts (which are wing/modal/sum-gate/anchor-hour/seasonality-by-month focused):

  A. CALIBRATION (favorite-longshot): at the 1 PM anchor, realized win rate vs the market-IMPLIED prob
     (last trade) for EVERY contract, binned by price band, pooled-all-cities + Chicago. Deviation from
     the diagonal = mispricing; the signed gap (win_rate - price) is the per-$1 EV of BUYING that band
     (negative => overpriced, a SELL candidate). Tradeable only if |gap| > spread + fee (~2-3c).
  B. DAY-OF-WEEK: production market_wing+drop_lower_ask edge (c/$payout, real ceil fee) by weekday,
     Chicago, IS vs OOS. A real weekday effect must persist OOS (IS-only = noise).
  C. INTERIOR-vs-TAIL wing: does a wing whose 2 legs are both INTERIOR (neither is a <= / >= tail)
     cover better / cost less than a wing that includes a tail leg? Chicago, IS/OOS.

Usage:
  python scripts/idea_batch_screen.py                 # Chicago for B/C; all 20 cities pooled for A
  python scripts/idea_batch_screen.py --series KXHIGHNY
"""
from __future__ import annotations

import argparse
import sys
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import _dummy_pred, _snapshot
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config
from weather_alpha.fees import trade_fee_cents
from weather_alpha.strategy import run_wing_strategy

SPLIT = 0.80
PROD = dict(require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
            flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
            drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.90)


def _is_tail(spec: str) -> bool:
    return spec.startswith("<=") or spec.startswith(">=")


def events(series: str):
    """Yield (event_date, contracts_at_1pm, winner_ticker) for each settled day on the tape."""
    df = load_city(series)
    if df.empty:
        return
    tz = ZoneInfo(CITY[series][1])
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        anchor = pd.Timestamp(ev.year, ev.month, ev.day, 13, tz=tz).tz_convert("UTC")
        contracts = _snapshot(day, anchor)
        if len(contracts) >= 2:
            yield ev, contracts, w[0]


# ---- A. calibration -------------------------------------------------------------------------------
def collect_calibration(series_list) -> pd.DataFrame:
    rows = []
    for s in series_list:
        if not (BACKFILL / s).is_dir():
            continue
        for ev, contracts, winner in events(s):
            for c in contracts:
                if c.last_price is None:
                    continue
                rows.append({"series": s, "date": ev, "p": float(c.last_price),
                             "won": int(c.ticker == winner)})
    return pd.DataFrame(rows)


def report_calibration(d: pd.DataFrame, label: str):
    bands = [(0.0, 0.05), (0.05, 0.15), (0.15, 0.30), (0.30, 0.45), (0.45, 0.55),
             (0.55, 0.70), (0.70, 0.85), (0.85, 0.95), (0.95, 1.0001)]
    print(f"\n[A] CALIBRATION -- {label}  (N={len(d)} contract-observations)")
    print(f"  {'price band':<12}{'N':>7}{'mean_p':>8}{'win%':>8}{'gap(win-p)':>12}{'OOS gap':>10}")
    print("  " + "-" * 57)
    dates = sorted(d["date"].unique())
    cut = dates[int(len(dates) * SPLIT)] if dates else None
    for lo, hi in bands:
        b = d[(d["p"] >= lo) & (d["p"] < hi)]
        if len(b) < 20:
            continue
        gap = b["won"].mean() - b["p"].mean()
        oos = b[b["date"] >= cut]
        ogap = (oos["won"].mean() - oos["p"].mean()) if len(oos) >= 20 else float("nan")
        og = f"{ogap*100:>+9.1f}" if ogap == ogap else f"{'-':>9}"
        print(f"  {f'{lo:.2f}-{hi:.2f}':<12}{len(b):>7}{b['p'].mean():>8.2f}"
              f"{b['won'].mean()*100:>7.1f}{gap*100:>+12.1f}{og:>10}")
    print("  gap>0 => underpriced (buy edge); gap<0 => overpriced (sell edge). Tradeable only if")
    print("  |gap| exceeds spread+fee (~2-3c). A persistent OOS gap in the same direction is the signal.")


# ---- shared wing economics (proxy fill + REAL ceil fee, like chicago_oos_netfee) ------------------
def wing_fire(cfg, ev, contracts, winner):
    out = run_wing_strategy(cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0, **PROD)
    if not out.targets:
        return None
    legs = {t.ticker: t for t in out.targets}
    specs = {c.ticker: c.bucket_spec for c in contracts}
    n = out.targets[0].target_contracts
    cov = int(winner in legs)
    net_c = 0
    for t in out.targets:
        c = next(x for x in contracts if x.ticker == t.ticker)
        cost = int(round(c.yes_ask * 100)) * t.target_contracts
        fee = trade_fee_cents(c.yes_ask, t.target_contracts)
        net_c += (t.target_contracts * 100 if t.ticker == winner else 0) - cost - fee
    has_tail = any(_is_tail(specs[tk]) for tk in legs)
    return {"date": ev, "cov": cov, "net_c": net_c, "base": n, "has_tail": has_tail}


def _edge(rows):
    if not rows:
        return float("nan"), 0
    norm = np.array([r["net_c"] / r["base"] for r in rows])
    return norm.mean(), len(rows)


# ---- B. day-of-week -------------------------------------------------------------------------------
def report_dow(cfg, fired):
    print(f"\n[B] DAY-OF-WEEK -- Chicago market_wing edge (c/$payout, real ceil fee)")
    print(f"  {'dow':<5}{'N':>5}{'IS edge':>9}{'OOS edge':>10}{'WR%':>7}")
    print("  " + "-" * 36)
    dates = sorted({r["date"] for r in fired})
    cut = dates[int(len(dates) * SPLIT)]
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    for i, nm in enumerate(names):
        day = [r for r in fired if r["date"].dayofweek == i]
        if not day:
            continue
        e_is, _ = _edge([r for r in day if r["date"] < cut])
        e_oos, n_oos = _edge([r for r in day if r["date"] >= cut])
        wr = np.mean([r["cov"] for r in day]) * 100
        eis = f"{e_is:>+8.1f}" if e_is == e_is else f"{'-':>8}"
        eo = f"{e_oos:>+9.1f}" if e_oos == e_oos else f"{'-':>9}"
        print(f"  {nm:<5}{len(day):>5}{eis:>9}{eo:>10}{wr:>6.0f}")
    print("  A weekday effect is real only if a high IS edge PERSISTS OOS (else it's noise / p-hacking).")


# ---- C. interior vs tail --------------------------------------------------------------------------
def report_interior_tail(fired):
    print(f"\n[C] INTERIOR-vs-TAIL wing -- Chicago (does avoiding the 2 tail buckets help?)")
    print(f"  {'cohort':<16}{'N':>5}{'WR%':>7}{'IS edge':>9}{'OOS edge':>10}")
    print("  " + "-" * 47)
    dates = sorted({r["date"] for r in fired})
    cut = dates[int(len(dates) * SPLIT)]
    for label, sel in (("interior-only", lambda r: not r["has_tail"]),
                       ("includes-tail", lambda r: r["has_tail"])):
        grp = [r for r in fired if sel(r)]
        if not grp:
            print(f"  {label:<16}{0:>5}"); continue
        e_is, _ = _edge([r for r in grp if r["date"] < cut])
        e_oos, _ = _edge([r for r in grp if r["date"] >= cut])
        wr = np.mean([r["cov"] for r in grp]) * 100
        eis = f"{e_is:>+8.1f}" if e_is == e_is else f"{'-':>8}"
        eo = f"{e_oos:>+9.1f}" if e_oos == e_oos else f"{'-':>9}"
        print(f"  {label:<16}{len(grp):>5}{wr:>6.0f}{eis:>9}{eo:>10}")


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--series", default="KXHIGHCHI", help="city for the wing screens B/C")
    args = ap.parse_args(argv)
    cfg = load_config()
    all_series = [s for s in CITY if (BACKFILL / s).is_dir()]

    print("=" * 64)
    print(f"MODEL-FREE IDEA BATCH #1  (deep tape, IS/OOS 80/20)  series={args.series}")
    print("=" * 64)

    # A: calibration pooled across all cities, and the focus city alone
    cal = collect_calibration(all_series)
    if not cal.empty:
        report_calibration(cal, f"ALL {cal['series'].nunique()} cities pooled")
        report_calibration(cal[cal["series"] == args.series], CITY[args.series][0])

    # B + C: wing fires on the focus city
    fired = [r for r in (wing_fire(cfg, ev, c, w) for ev, c, w in events(args.series)) if r]
    if fired:
        report_dow(cfg, fired)
        report_interior_tail(fired)
    else:
        print(f"\n(no wing fires on {args.series} -- skipping B/C)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
