"""Walk-forward OOS test of a PER-CITY edge gate vs the global 0.90 gate, with/without a bounded
intraday monitoring window. Answers: does selecting cities by their OWN (past-only) coverage beat
the status quo out of sample, and does watching past 1 PM add anything on top?

DESIGN (no look-ahead):
  Step 0  gate-free wing table: per city x settled-day x offset {0,15,30,60} min past 1 PM, build
          the production 2-leg wing with the cost gate OFF (max_ask_sum=0.999) and store cost
          (sum asks), fee (per-$1), covered, and the flat-$2.50 day-PnL of entering then.
  Step 1  order each city's days by date.
  Step 2  expanding estimate assumed_cov_city(t) = covered/fired over PRIOR days that fired under
          the baseline gate (cost<=0.90 @1PM). Warmup: not scored until >=WARMUP prior fired days.
          t's own outcome is added to the estimate only AFTER t is decided.
  Step 3  four variants on the SAME post-warmup test days:
            B  1PM only,  fire iff cost<=0.90                    (status quo)
            S  1PM only,  fire iff cost+fee<=assumed_cov_city(t) (per-city selection)
            W  scan 0..60,fire at first cost<=0.90               (monitoring only)
            SW scan 0..60,fire at first cost+fee<=assumed_cov(t) (selection + monitoring)
  Step 4  pool test days: total PnL, trades, coverage, edge/trade. S-B = selection; W-B =
          monitoring; SW-S = monitoring on top of selection.

CAVEATS: one season -> thin per-city test sets; last-trade proxy understates afternoon sharpening;
gate uses the fee approximation, PnL uses exact fees; 100% fill assumed. Directional, not final.
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

WINDOW = [0, 15, 30, 60]      # entry-scan offsets (minutes past 1 PM), <= +60 min
BASE_GATE = 0.90              # status-quo max_ask_sum (on sum of asks)
WARMUP = 10                   # prior baseline-fired days required before a city is scored
LOOSE = 0.999                 # gate-free wing construction so any threshold can be applied post-hoc
VARIANTS = ["B", "S", "W", "SW"]


def _metrics(contracts, out, winner) -> dict | None:
    leg_asks, cov, pnl_c = [], 0, 0
    for tgt in out.targets:
        c = next((x for x in contracts if x.ticker == tgt.ticker), None)
        if c is None:
            continue
        price = c.yes_ask if tgt.side == "yes" else c.no_ask
        n = tgt.target_contracts
        won = (tgt.ticker == winner) if tgt.side == "yes" else (tgt.ticker != winner)
        pnl_c += (n * 100 if won else 0) - int(round(price * 100)) * n - trade_fee_cents(price, n)
        leg_asks.append(price); cov = cov or int(won)
    if not leg_asks:
        return None
    return {"cost": float(sum(leg_asks)), "fee1": float(sum(0.07 * a * (1 - a) for a in leg_asks)),
            "cov": int(cov), "pnl": pnl_c / 100.0}


def build_table(cfg) -> dict:
    """series -> chronological list of (event_date, {offset: metrics}); only days with a 1 PM book."""
    tab = {}
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        tz = ZoneInfo(CITY[s][1])
        days = []
        for ev, day in df.groupby("event_date"):
            w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
            if len(w) != 1:
                continue
            winner = w[0]
            base = pd.Timestamp(ev.year, ev.month, ev.day, 13, tz=tz).tz_convert("UTC")
            om = {}
            for off in WINDOW:
                contracts = _snapshot(day, base + pd.Timedelta(minutes=off))
                if len(contracts) < 2:
                    continue
                out = run_wing_strategy(
                    cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
                    require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
                    flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
                    drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=LOOSE)
                if not out.targets:
                    continue
                m = _metrics(contracts, out, winner)
                if m:
                    om[off] = m
            if 0 in om:
                days.append((ev, om))
        days.sort(key=lambda x: x[0])
        if days:
            tab[s] = days
    return tab


def _first(om, pred):
    for off in WINDOW:
        m = om.get(off)
        if m and pred(m):
            return m
    return None


def _add(acc, m):
    acc["pnl"] += m["pnl"]; acc["trades"] += 1; acc["cov"] += m["cov"]


def run():
    cfg = load_config()
    tab = build_table(cfg)

    agg = {v: {"pnl": 0.0, "trades": 0, "cov": 0} for v in VARIANTS}
    test_days = 0
    percity = {}

    for s, days in tab.items():
        csum = cn = 0                                  # prior baseline-fired covered / count
        pc = {v: {"pnl": 0.0, "trades": 0, "cov": 0} for v in VARIANTS}
        pc_days = 0
        for ev, om in days:
            cov_est = (csum / cn) if cn else None
            if cn >= WARMUP:                            # scored test day
                test_days += 1; pc_days += 1
                m0 = om[0]
                if m0["cost"] < BASE_GATE:
                    _add(agg["B"], m0); _add(pc["B"], m0)
                if m0["cost"] + m0["fee1"] <= cov_est:
                    _add(agg["S"], m0); _add(pc["S"], m0)
                mw = _first(om, lambda m: m["cost"] < BASE_GATE)
                if mw:
                    _add(agg["W"], mw); _add(pc["W"], mw)
                msw = _first(om, lambda m: m["cost"] + m["fee1"] <= cov_est)
                if msw:
                    _add(agg["SW"], msw); _add(pc["SW"], msw)
            if om[0]["cost"] < BASE_GATE:             # update estimate AFTER deciding this day
                csum += om[0]["cov"]; cn += 1
        pc["days"] = pc_days
        pc["cov_est"] = (csum / cn) if cn else None
        percity[s] = pc

    # ---- pooled table ----
    print("=" * 88)
    print(f"WALK-FORWARD per-city gate vs global 0.90  (warmup {WARMUP}, window <=60min, no shrinkage)")
    print(f"pooled across cities on {test_days} post-warmup TEST days")
    print("=" * 88)
    print(f"{'variant':<26}{'trades':>8}{'fire%':>7}{'cover':>7}{'PnL$':>10}{'edge/trade':>12}")
    lab = {"B": "B  baseline 1PM @0.90", "S": "S  per-city select 1PM",
           "W": "W  global + window", "SW": "SW select + window"}
    for v in VARIANTS:
        a = agg[v]; tr = a["trades"]
        fire = tr / test_days if test_days else 0
        cov = a["cov"] / tr if tr else float("nan")
        ept = a["pnl"] / tr if tr else float("nan")
        print(f"{lab[v]:<26}{tr:>8}{fire:>7.0%}{cov:>7.0%}{a['pnl']:>+10.2f}{ept:>+12.3f}")

    print("\nDECOMPOSITION (pooled PnL$):")
    print(f"  S  - B   (value of per-city SELECTION)        {agg['S']['pnl'] - agg['B']['pnl']:>+8.2f}")
    print(f"  W  - B   (value of MONITORING alone)          {agg['W']['pnl'] - agg['B']['pnl']:>+8.2f}")
    print(f"  SW - S   (monitoring ON TOP of selection)     {agg['SW']['pnl'] - agg['S']['pnl']:>+8.2f}")
    print(f"  SW - B   (full proposed vs status quo)        {agg['SW']['pnl'] - agg['B']['pnl']:>+8.2f}")

    # ---- per-city ----
    print("\nPER-CITY (post-warmup test days only; PnL$ with trade count):")
    print(f"{'city':<15}{'nTest':>6}{'covEst':>8}{'B':>14}{'S':>14}{'SW':>14}")
    rows = sorted(percity.items(), key=lambda kv: -(kv[1]["cov_est"] or 0))
    for s, pc in rows:
        if pc["days"] == 0:
            continue
        ce = f"{pc['cov_est']:.0%}" if pc["cov_est"] is not None else "  --"
        def cell(v):
            return f"{pc[v]['pnl']:>+8.2f}({pc[v]['trades']:>2})"
        print(f"{CITY[s][0]:<15}{pc['days']:>6}{ce:>8}{cell('B'):>14}{cell('S'):>14}{cell('SW'):>14}")
    nz = [s for s, pc in percity.items() if pc["days"] == 0]
    if nz:
        print(f"({len(nz)} cities never cleared warmup -> 0 test days: "
              f"{', '.join(CITY[s][0] for s in nz)})")

    print("\nReading: S-B>0 => per-city selection survives OOS; SW-S>0 => the bounded window adds.")
    print("Thin single-season test sets -> directional; confirm with forward paper trading.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
