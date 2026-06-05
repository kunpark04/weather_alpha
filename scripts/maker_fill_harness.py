"""Maker-fill harness: would POSTING AT THE BID (maker) beat LIFTING THE ASK (taker)?

The production wing is a TAKER: it places a limit order AT the ask, crosses the spread, and
(near-)always fills. The spread (~1-2c/leg) is what eats the edge for 19 of 20 cities. The one
lever that could change that is MAKER execution -- rest a bid and let the market come to you,
saving the spread -- but a maker only fills when someone SELLS into the bid, and those fills are
adversely selected (the market often sells because the bucket is turning into a loser).

This harness answers "does posting at the bid fill, and does it help?" directly on the DEEP
trade tape -- no need to wait for the order-book logger to accumulate -- using a fact the tape
uniquely gives us: `taker_side`.

  taker_side == "yes"  -> aggressor BOUGHT yes = lifted the ASK  (a resting SELLER filled)
  taker_side == "no"   -> aggressor SOLD  yes = hit  the BID     (a resting BUYER  filled)  <-- US

So a maker who rests a YES buy at price P fills iff a `taker_side=="no"` trade prints at
yes_price <= P within the post-anchor window: real selling pressure reached our bid.

WHAT IT COMPUTES (per city, IN-SAMPLE and OOS, 80/20 by date; leg level):
  fillM%      maker fill rate at the bid (post = last_trade<=anchor - offset)  <- the headline
  winFill%/   maker fill rate split by whether the leg WON vs LOST -- the adverse-selection test:
  lossFill%     if winners fill LESS than losers, maker silently drops the payoff legs
  taker edge   realistic taker: pay the NEXT actual trade after anchor (what we pay live), IS/OOS
  maker edge   over FILLED legs only: coverage - post_price - fee, IS/OOS
  effYr        maker fills/yr = legs/yr * fillM%  (the 'N' the small-edge thesis needs)
Edge = coverage - cost - fee, cents per $1 payout; fee = 0.07*p*(1-p)/leg (continuous approx,
matching scripts/per_city_synthesis.py so the taker columns line up with that analysis).

ASSUMPTIONS / LIMITS (honest):
  * Post price = last trade <= anchor - `offset` cents (default 1 = "join the bid"). The backfill
    has NO historical quotes, so the bid is derived from the last print; offset brackets it.
  * Fill model = "the market traded down into our bid" (a no-taker sell <= P). It does NOT model
    QUEUE POSITION or SIZE (if others rest at P and exhaust the sellers first, we might not fill)
    -> fill% here is an UPPER bound on a single small order. The order-book logger (real depth +
    queue) is the forward upgrade that tightens this; see scripts/orderbook_logger.py.
  * Leg SELECTION is unchanged from production (run_wing_strategy on the last+1c snapshot), so
    only the EXECUTION model differs -- a clean taker-vs-maker A/B on identical legs/days.

Usage:
  python scripts/maker_fill_harness.py                 # offset=1 (post at the bid), 60-min window
  python scripts/maker_fill_harness.py --offset 0      # post AT the last print (more aggressive)
  python scripts/maker_fill_harness.py --window 30     # tighter post-anchor fill window (minutes)
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

from scripts.backtest_multicity import _dummy_pred, _snapshot
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config
from weather_alpha.strategy import run_wing_strategy

FEE1 = lambda a: 0.07 * a * (1 - a)   # continuous per-leg fee approx (matches per_city_synthesis)
SPLIT = 0.80


def per_day_legs(df: pd.DataFrame, tz, cfg, *, window_min: int, offset_c: int) -> pd.DataFrame:
    """One row per wing LEG per day, with taker + maker economics. Identical leg selection to
    production; only the execution model is added."""
    window = pd.Timedelta(minutes=window_min)
    recs = []
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        winner = w[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        anchor = pd.Timestamp(ev.year, ev.month, ev.day, 13, tz=tz).tz_convert("UTC")
        contracts = _snapshot(day, anchor)
        if len(contracts) < 2:
            continue
        res = run_wing_strategy(
            cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
            require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
            flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
            drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.90)
        if not res.targets:
            continue
        legs = [t.ticker for t in res.targets]
        fut = day[(day["timestamp"] > anchor) & (day["timestamp"] <= anchor + window)]
        for tk in legs:
            c = next(x for x in contracts if x.ticker == tk)
            last = c.last_price                              # yes price of the last trade <= anchor
            P = max(round((last - offset_c / 100.0), 2), 0.01)   # maker bid post (cents below last)
            tkfut = fut[fut["ticker"] == tk].sort_values("timestamp")
            # taker-REALISTIC: pay the next actual trade after the anchor (what lifting costs live)
            tk_real = tkfut["yes_price_cents"].iloc[0] / 100.0 if len(tkfut) else np.nan
            # maker fill: a no-taker SELL printed at yes_price <= our bid P within the window
            Pc = round(P * 100)
            sells = tkfut[(tkfut["taker_side"] == "no") & (tkfut["yes_price_cents"] <= Pc)]
            recs.append({
                "date": ev, "year": ev.year, "leg": tk, "win": int(tk == winner),
                "ask_proxy": min(last + 0.01, 0.99), "post": P, "tk_real": tk_real,
                "has_real": len(tkfut) > 0, "maker_fill": int(len(sells) > 0),
            })
    return pd.DataFrame(recs)


def _edge(d: pd.DataFrame, cost_col: str, mask=None) -> float:
    """coverage - cost - fee over the masked subset, cents per $1."""
    sub = d if mask is None else d[mask]
    sub = sub.dropna(subset=[cost_col])
    if sub.empty:
        return np.nan
    return (sub["win"].mean() - sub[cost_col].mean() - FEE1(sub[cost_col]).mean()) * 100


def summarize(d: pd.DataFrame) -> dict:
    is_d, oos_d = d[d["date"] < d["_cut"]], d[d["date"] >= d["_cut"]]
    fills = d["maker_fill"] == 1
    win = d["win"] == 1
    return {
        "legs": len(d),
        "fillM": d["maker_fill"].mean(),
        "winFill": d.loc[win, "maker_fill"].mean() if win.any() else np.nan,
        "lossFill": d.loc[~win, "maker_fill"].mean() if (~win).any() else np.nan,
        # realistic TAKER edge (next trade), IS / OOS
        "tk_is": _edge(is_d, "tk_real"), "tk_oos": _edge(oos_d, "tk_real"),
        # MAKER edge over FILLED legs only, IS / OOS
        "mk_is": _edge(is_d[is_d["maker_fill"] == 1], "post"),
        "mk_oos": _edge(oos_d[oos_d["maker_fill"] == 1], "post"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=int, default=1, help="cents below last to post the bid (default 1)")
    ap.add_argument("--window", type=int, default=60, help="post-anchor fill window, minutes (default 60)")
    args = ap.parse_args(argv)

    cfg = load_config()
    rows = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        d = per_day_legs(df, ZoneInfo(CITY[s][1]), cfg, window_min=args.window, offset_c=args.offset)
        if d.empty:
            continue
        dates = sorted(d["date"].unique())
        span_yr = max((dates[-1] - dates[0]).days / 365.0, 0.25)
        d = d.copy()
        d["_cut"] = dates[int(len(dates) * SPLIT)]
        m = summarize(d)
        m["city"] = CITY[s][0]
        m["legsYr"] = len(d) / span_yr
        m["effYr"] = len(d) / span_yr * m["fillM"]
        rows.append(m)

    rows.sort(key=lambda r: -(r["mk_oos"] if r["mk_oos"] == r["mk_oos"] else -99))
    print("=" * 122)
    print(f"MAKER (post at bid = last-{args.offset}c) vs TAKER (next trade), {args.window}-min window  "
          f"-- edge c/$1, IS & OOS, leg level")
    print("=" * 122)
    print(f"{'city':<14}{'legs':>5}{'legs/yr':>8}{'fillM%':>7}{'winFil%':>8}{'losFil%':>8}{'effYr':>6}"
          f"{'  | ':>4}{'tkIS':>7}{'tkOOS':>7}{'  ':>2}{'mkIS':>7}{'mkOOS':>7}")
    for r in rows:
        def c(x):
            return f"{x:>+7.1f}" if x == x else f"{'-':>7}"
        print(f"{r['city']:<14}{r['legs']:>5}{r['legsYr']:>8.0f}{r['fillM']:>6.0%} "
              f"{r['winFill']:>7.0%} {r['lossFill']:>7.0%}{r['effYr']:>6.0f}{'  | ':>4}"
              f"{c(r['tk_is'])}{c(r['tk_oos'])}{'  ':>2}{c(r['mk_is'])}{c(r['mk_oos'])}")
    print("\nKEY: fillM% = maker fill rate at the bid (the headline). winFil%/losFil% = fill rate on")
    print("WINNING vs LOSING legs -- if winFil < losFil, maker is adversely selected (drops payoffs).")
    print("tkIS/tkOOS = realistic taker edge (pay next trade). mkIS/mkOOS = maker edge over FILLED legs.")
    print("A maker WIN needs: mkOOS > tkOOS AND winFil% high enough that you still catch the payoff legs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
