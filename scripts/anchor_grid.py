"""Grid search: cities x anchor time (12:00..16:00 local, 30-min steps) for the production wing.

For every city and every anchor, reconstruct the book at that local time and run the live wing
(market_wing, drop_lower_ask, gate<0.90, flat $2.50) over the city's full 3.4yr history. Emits:
  1. EDGE grid   (cents per $1 payout = coverage - cost - fee)  -- anchor QUALITY, fire-count-neutral
  2. COVER grid  (winner inside the 2-leg wing, on fired days)  -- exposes realized-high leakage
  3. 1 PM assumptions diagnostic + each city's best TRADEABLE anchor (restricted to 12:00-14:00)

WHY edge not PnL in the grid: later anchors FIRE more, so PnL grows with fire-count even at equal
quality; edge (per $1) isolates anchor quality. The PnL total is in the diagnostic.

CRITICAL CAVEAT -- realized-high leakage: settlement = the day's MAX temp. As the anchor moves into
the afternoon the high is progressively REALIZED, so coverage/edge rising past ~14:00 is largely the
market pricing an outcome already (partly) known, NOT a tradeable forecast edge; liquidity at 15-16h
is also thinner than the +1c proxy assumes. Treat 12:00-14:00 as the genuine forecast window; the
15:00-16:00 columns are shown for completeness and are NOT actionable. In-sample/exploratory: any
anchor that beats 1 PM must be OOS-validated before belief (don't overfit the anchor).
"""
from __future__ import annotations

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

ANCHORS = [(12, 0), (12, 30), (13, 0), (13, 30), (14, 0), (14, 30), (15, 0), (15, 30), (16, 0)]
TRADEABLE = {(12, 0), (12, 30), (13, 0), (13, 30), (14, 0)}     # pre-leakage forecast window
GATE = 0.90
FEE1 = lambda a: 0.07 * a * (1 - a)


def alab(h, m):
    return f"{(h - 12) or 12}:{m:02d}"


def wing_day(day, winner, anchor_utc, cfg, ev):
    contracts = _snapshot(day, anchor_utc)
    if len(contracts) < 2:
        return None
    modal = max(contracts, key=lambda c: c.yes_ask)
    rec = {"modal_correct": int(modal.ticker == winner), "fired": 0}
    out = run_wing_strategy(
        cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
        require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
        flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
        drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=GATE)
    if not out.targets:
        return rec
    legs, cov, pnl = [], 0, 0
    for tgt in out.targets:
        c = next((x for x in contracts if x.ticker == tgt.ticker), None)
        if c is None:
            continue
        a, n = c.yes_ask, tgt.target_contracts
        won = tgt.ticker == winner
        pnl += (n * 100 if won else 0) - round(a * 100) * n - trade_fee_cents(a, n)
        legs.append(a); cov = cov or int(won)
    rec.update({"fired": 1, "cov": cov, "cost": float(sum(legs)),
                "fee": float(sum(FEE1(a) for a in legs)), "pnl": pnl / 100.0})
    return rec


def city_anchor(df, s, cfg, tz, h, m):
    fired = covered = npre = mod = 0
    costs, fees, pnls = [], [], []
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        anchor = pd.Timestamp(ev.year, ev.month, ev.day, h, m, tz=tz).tz_convert("UTC")
        r = wing_day(day, w[0], anchor, cfg, ev)
        if r is None:
            continue
        npre += 1; mod += r["modal_correct"]
        if r["fired"]:
            fired += 1; covered += r["cov"]
            costs.append(r["cost"]); fees.append(r["fee"]); pnls.append(r["pnl"])
    if fired == 0:
        return {"fire": 0.0, "npre": npre, "fired": 0, "modal": mod / npre if npre else np.nan}
    cov = covered / fired
    cost, fee = float(np.mean(costs)), float(np.mean(fees))
    return {"fire": fired / npre, "npre": npre, "fired": fired, "cov": cov,
            "cost": cost, "edge": cov - cost - fee, "pnl": float(np.sum(pnls)),
            "modal": mod / npre if npre else np.nan}


def main():
    cfg = load_config()
    grid = {}                                  # series -> {(h,m): stats}
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        tz = ZoneInfo(CITY[s][1])
        grid[s] = {a: city_anchor(df, s, cfg, tz, *a) for a in ANCHORS}
        print(f"  done {CITY[s][0]}", flush=True)

    order = sorted(grid, key=lambda s: -(grid[s][(13, 0)].get("edge", -9)))
    cols = "".join(alab(h, m).rjust(8) for h, m in ANCHORS)

    def emit(title, key, fmt, scale=1.0, pct=False):
        print("\n" + "=" * 96)
        print(title)
        print("=" * 96)
        print(f"{'city':<14}{cols}   best(<=2pm)")
        for s in order:
            cells = ""
            best, bv = None, -9e9
            for a in ANCHORS:
                v = grid[s][a].get(key)
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    cells += f"{'-':>8}"
                    continue
                cells += (f"{v*scale:>7.0%}" + " ") if pct else f"{v * scale:>+8.1f}"
                if a in TRADEABLE and v > bv:
                    best, bv = a, v
            bl = f"  {alab(*best)} ({bv*scale:+.1f})" if best else ""
            print(f"{CITY[s][0]:<14}{cells}{bl}")

    emit("EDGE by anchor  (cents per $1 payout = coverage - cost - fee; +=beats price)", "edge",
         "+.1f", scale=100.0)
    emit("COVERAGE by anchor  (winner inside 2-leg wing, fired days) -- rising late = realized-high",
         "cov", ".0%", pct=True)

    # 1 PM assumptions diagnostic
    print("\n" + "=" * 96)
    print("AT 1 PM: assumptions diagnostic  (assumed_win_prob 0.92 is INERT on flat; the binding")
    print("assumptions are anchor / market-modal center / gate<0.90 / drop_lower)")
    print("=" * 96)
    print(f"{'city':<14}{'fire%':>6}{'cover':>7}{'breakeven':>10}{'edgeC/$':>8}{'modalOK':>8}"
          f"{'1PM_PnL':>9}   best<=2pm vs 1PM")
    for s in order:
        d13 = grid[s][(13, 0)]
        if d13["fired"] == 0:
            continue
        be = d13["cost"] + (d13["cov"] - d13["cost"] - d13["edge"])      # cost+fee
        # best tradeable edge
        bestA, bestE = max(((a, grid[s][a].get("edge", -9)) for a in TRADEABLE),
                           key=lambda x: x[1])
        gain = (bestE - d13["edge"]) * 100
        tag = f"{alab(*bestA)} ({bestE*100:+.1f}, {gain:+.1f} vs 1PM)" if bestA != (13, 0) else "1PM is best"
        print(f"{CITY[s][0]:<14}{d13['fire']:>6.0%}{d13['cov']:>7.0%}{be:>10.3f}"
              f"{d13['edge']*100:>+8.1f}{d13['modal']:>8.0%}{d13['pnl']:>+9.2f}   {tag}")

    print("\nREAD: assumed_win_prob 0.92 never binds on flat (gate 0.90 is stricter), so it is NOT why")
    print("any city loses. Losers share the cost structure (~0.80) but undershoot on COVERAGE -- the")
    print("intrinsic-forecastability deficit, not a wrong constant. If a city's best<=2pm >> its 1PM,")
    print("the ANCHOR assumption is suboptimal for it (OOS-validate before changing). 15-16h columns")
    print("are realized-high leakage, NOT tradeable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
