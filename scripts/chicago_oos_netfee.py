"""Net-of-(REAL ceil)-fee OOS replay of the EXACT production config on the Chicago tape.

Closes the gap an LLM-council review flagged (tasks/council-transcript-2026-06-07-improve-vs-pivot.md):
`scripts/per_city_synthesis.py` reports Chicago's edge using a CONTINUOUS fee approximation
(FEE1 = 0.07*a*(1-a)) in normalized cents-per-$1-payout, and its own docstring admits the real
ceil() fee is "a touch optimistic" at small N. This script re-runs the SAME production strategy fn
(`run_wing_strategy`, market_wing + drop_lower_ask, flat $2.50, fee_aware) with the REAL
`weather_alpha.fees.trade_fee_cents` ceil() fee, in ACTUAL flat-$2.50 dollars, split 80/20 IS/OOS
exactly as per_city_synthesis does, under BOTH fill assumptions:

  PROXY     (optimistic) : entry at yes_ask = last-trade + 1c, 100% fill. This is the assumption
                           behind the +5.0c/$1 headline.
  REALISTIC (pessimistic): entry at the FIRST trade printed in the 60-min post-anchor window; a day
                           counts only if EVERY leg had a subsequent print (bakes in spread +
                           adverse selection). Same contract counts as the strategy chose.

Reports, per (segment x fill): fired days, fillable days, win rate, total $ PnL (flat $2.50, real
ceil fee), mean cents/trade, and the normalized cents-per-$1-payout edge (directly comparable to the
+5.0c headline) with its per-day standard error and t-stat. The decision question: does Chicago's
edge clear 0 OOS, net of the REAL fee, under the realistic-fill bound?

Truth = Kalshi settlement_value. Hold to settlement, no intraday exits. Single city (KXHIGHCHI).
Run:  python scripts/chicago_oos_netfee.py            # Chicago (default)
      python scripts/chicago_oos_netfee.py KXHIGHNY   # any backfilled series, for contrast
"""
from __future__ import annotations

import math
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
from weather_alpha.fees import trade_fee_cents
from weather_alpha.strategy import run_wing_strategy

WINDOW = pd.Timedelta(minutes=60)
SPLIT = 0.80


def per_day(df: pd.DataFrame, tz: ZoneInfo, cfg) -> pd.DataFrame:
    """One row per day the PRODUCTION strategy fires. Dollars use the real ceil() fee."""
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
        # EXACT production config -- identical kwargs to scripts/backtest_multicity.run_one and
        # the live config (config/live.yaml): market_wing + drop_lower_ask, flat $2.50, fee_aware.
        out = run_wing_strategy(
            cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
            require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
            flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
            drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.90,
        )
        if not out.targets:
            continue
        legs = [t.ticker for t in out.targets]
        cov = int(winner in legs)

        fut = day[(day["timestamp"] > anchor) & (day["timestamp"] <= anchor + WINDOW)]
        p_net_c = 0          # proxy (optimistic) net cents, real ceil fee
        r_net_c = 0          # realistic (pessimistic) net cents, real ceil fee
        p_cost_c = p_fee_c = 0
        r_cost_c = r_fee_c = 0
        fillable = True
        n_leg = out.targets[0].target_contracts   # equal_payout => identical across legs

        for t in out.targets:
            c = next(x for x in contracts if x.ticker == t.ticker)
            n = t.target_contracts
            won = t.ticker == winner            # wing legs are all side='yes'

            # PROXY: fill at the +1c ask proxy, 100% fill.
            pp = c.yes_ask
            pc = int(round(pp * 100)) * n
            pf = trade_fee_cents(pp, n)
            p_cost_c += pc
            p_fee_c += pf
            p_net_c += (n * 100 if won else 0) - pc - pf

            # REALISTIC: fill at the first post-anchor print within the window (else day unfillable).
            tr = fut.loc[fut["ticker"] == t.ticker, "yes_price_cents"]
            if len(tr):
                rp = tr.iloc[0] / 100.0
                rc = int(round(rp * 100)) * n
                rf = trade_fee_cents(rp, n)
                r_cost_c += rc
                r_fee_c += rf
                r_net_c += (n * 100 if won else 0) - rc - rf
            else:
                fillable = False

        recs.append({
            "date": ev, "cov": cov, "n_leg": n_leg,
            "p_net_c": p_net_c, "p_cost_c": p_cost_c, "p_fee_c": p_fee_c,
            "r_net_c": r_net_c if fillable else np.nan,
            "r_cost_c": r_cost_c if fillable else np.nan,
            "r_fee_c": r_fee_c if fillable else np.nan,
            "fillable": fillable,
        })
    return pd.DataFrame(recs)


def _seg_stats(d: pd.DataFrame, fill: str) -> dict:
    """Aggregate one segment under one fill model. `fill` in {'proxy','real'}."""
    if fill == "real":
        d = d[d["fillable"]]
    net_c = d["p_net_c"] if fill == "proxy" else d["r_net_c"]
    fee_c = d["p_fee_c"] if fill == "proxy" else d["r_fee_c"]
    cost_c = d["p_cost_c"] if fill == "proxy" else d["r_cost_c"]
    n = len(d)
    if n == 0:
        return {"n": 0}
    norm = net_c / d["n_leg"]                 # cents per $1 of payout-if-win (n_leg contracts win $1 each)
    se = norm.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
    mean_norm = norm.mean()
    # Favorite-longshot framing: WR vs the per-day breakeven win rate (cost+fee per $1 payout).
    # One-sided exact binomial p that the observed coverage beats the mean breakeven -- the
    # structurally-correct test for a high-WR coverage bet (complements the dollar t-test).
    be = ((cost_c + fee_c) / (d["n_leg"] * 100.0)).mean()
    wins = int(d["cov"].sum())
    p0 = min(max(be, 1e-9), 1 - 1e-9)
    binp = math.fsum(math.comb(n, k) * p0 ** k * (1 - p0) ** (n - k)
                     for k in range(wins, n + 1))
    return {
        "n": n,
        "wr": d["cov"].mean(),
        "pnl_usd": net_c.sum() / 100.0,
        "fees_usd": fee_c.sum() / 100.0,
        "mean_c_trade": net_c.mean(),         # mean cents per trade (actual flat-$2.50 economics)
        "edge_c_payout": mean_norm,           # normalized -- compare to the +5.0c headline
        "se": se,
        "t": (mean_norm / se) if se and se == se and se > 0 else np.nan,
        "be": be,
        "binp": binp,
    }


def _fmt(s: dict) -> str:
    if s.get("n", 0) == 0:
        return f"{'(none)':>66}"
    t = f"{s['t']:>+5.1f}" if s["t"] == s["t"] else f"{'-':>5}"
    se = f"{s['se']:>4.1f}" if s["se"] == s["se"] else f"{'-':>4}"
    return (f"{s['n']:>4}{s['wr']:>7.0%}{s['pnl_usd']:>+9.2f}{s['fees_usd']:>8.2f}"
            f"{s['mean_c_trade']:>+9.1f}{s['edge_c_payout']:>+9.1f}{se:>6}{t:>6}"
            f"{s['be']:>7.0%}{s['binp']:>7.3f}")


def main(argv) -> int:
    series = (argv[0] if argv else "KXHIGHCHI")
    cfg = load_config()
    if not (BACKFILL / series).is_dir():
        print(f"no backfill for {series} under {BACKFILL}")
        return 1
    df = load_city(series)
    if df.empty:
        print(f"empty tape for {series}")
        return 1
    tz = ZoneInfo(CITY[series][1])
    d = per_day(df, tz, cfg)
    if d.empty:
        print(f"{CITY[series][0]}: strategy never fired on this tape")
        return 1

    dates = sorted(d["date"].unique())
    cut = dates[int(len(dates) * SPLIT)]
    span_yr = max((dates[-1] - dates[0]).days / 365.0, 0.25)
    segs = {
        "ALL": d,
        "IS (first 80%)": d[d["date"] < cut],
        "OOS (last 20%)": d[d["date"] >= cut],
    }

    print("=" * 96)
    print(f"{CITY[series][0]} ({series})  --  EXACT production config, REAL ceil() fee, flat $2.50")
    print(f"tape {dates[0].date()} -> {dates[-1].date()}  ({span_yr:.1f} yr)  |  fired {len(d)} days"
          f"  ({len(d)/span_yr:.0f}/yr)  |  OOS cut at {cut.date()}")
    print("=" * 96)
    hdr = (f"{'segment':<16}{'fill':<6}{'N':>4}{'WR':>7}{'PnL$':>9}{'fees$':>8}{'c/trade':>9}"
           f"{'c/$payout':>9}{'SE':>6}{'t':>6}{'BE%':>7}{'binP':>7}")
    print(hdr)
    print("-" * len(hdr))
    for name, seg in segs.items():
        for fill, label in (("proxy", "proxy"), ("real", "real")):
            print(f"{name:<16}{label:<6}{_fmt(_seg_stats(seg, fill))}")
        print()

    print("KEY: c/$payout = net cents per $1 of payout-if-win = the normalized edge directly")
    print("     comparable to per_city_synthesis's +5.0c (which used a CONTINUOUS fee approx).")
    print("     proxy = 100% fill at last+1c ask (optimistic). real = fill at first post-anchor")
    print("     print within 60 min, only on days every leg printed (adverse selection).")
    print("     t = mean / per-day SE; |t|>~2 => OOS edge distinguishable from 0 at this N.")
    print("     BE% = mean per-day breakeven win rate (cost+fee per $payout); binP = one-sided exact")
    print("     binomial p that WR beats BE% (favorite-longshot test; p>0.05 => not significant).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
