"""Walk-forward, EXTENDED to the full 3-leg wing + the hybrid the user actually proposed.

Prior walkforward_percity_gate.py only tested the 2-leg wing in the window. This adds the full
wing and the exact proposed hybrid: 2-wing when it has per-city edge, ELSE full wing at <0.90,
monitored across 1:00..2:00. Same no-look-ahead expanding estimate, same warmup, same test days.

Diagnostic first: 2-leg vs 3-leg wing coverage / cost / edge on their own 0.90-fired sets at 1 PM
-- shows whether the full wing's higher coverage is eaten by its higher cost (the drop_lower_ask
premise) or genuinely clears breakeven.

Variants on the same post-warmup test days:
  B   2-wing, 1PM, cost<=0.90                                  (status quo)
  S   2-wing, 1PM, cost+fee<=cov2_est                          (per-city select, from before)
  F   FULL wing, 1PM, cost3<=0.90                              (full-wing standalone)
  FW  FULL wing, window, first cost3<=0.90                     (full-wing monitored)
  H   window: 2-wing if cost2+fee<=cov2_est ELSE full if cost3<=0.90, first hit  (THE PROPOSAL)

CAVEATS unchanged: one season, last-trade proxy understates afternoon sharpening, gate uses fee
approximation / PnL exact, 100% fill. Directional.
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

WINDOW = [0, 15, 30, 60]
BASE_GATE = 0.90
WARMUP = 10
LOOSE = 0.999
VARIANTS = ["B", "S", "F", "FW", "H", "H0"]


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
            "cov": int(cov), "pnl": pnl_c / 100.0, "legs": len(leg_asks)}


def _wing(cfg, ev, contracts, winner, drop_lower):
    out = run_wing_strategy(
        cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
        require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
        flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
        drop_lower_ask=drop_lower, drop_higher_ask=False, max_ask_sum=LOOSE)
    return _metrics(contracts, out, winner) if out.targets else None


def build_table(cfg) -> dict:
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
                cell = {}
                m2 = _wing(cfg, ev, contracts, winner, True)
                m3 = _wing(cfg, ev, contracts, winner, False)
                if m2:
                    cell["w2"] = m2
                if m3:
                    cell["w3"] = m3
                if "w2" in cell:
                    om[off] = cell
            if 0 in om and "w2" in om[0]:
                days.append((ev, om))
        days.sort(key=lambda x: x[0])
        if days:
            tab[s] = days
    return tab


def _add(acc, m):
    acc["pnl"] += m["pnl"]; acc["trades"] += 1; acc["cov"] += m["cov"]


def _first_full(om):
    for off in WINDOW:
        d = om.get(off)
        if d and "w3" in d and d["w3"]["cost"] < BASE_GATE:
            return d["w3"]
    return None


def _hybrid(om, cov2):
    for off in WINDOW:
        d = om.get(off)
        if not d:
            continue
        m2 = d.get("w2")
        if m2 and m2["cost"] + m2["fee1"] <= cov2:
            return m2
        m3 = d.get("w3")
        if m3 and m3["cost"] < BASE_GATE:
            return m3
    return None


def diagnostic(tab):
    """2-leg vs 3-leg wing on their own 0.90-fired sets at 1 PM (pooled)."""
    acc = {"w2": [], "w3": []}
    for days in tab.values():
        for _, om in days:
            for k in ("w2", "w3"):
                m = om[0].get(k)
                if m and m["cost"] < BASE_GATE:
                    acc[k].append(m)
    print("DIAGNOSTIC -- 2-leg vs FULL 3-leg wing at 1 PM, each on its own cost<=0.90 fired set:")
    print(f"{'wing':<8}{'nFired':>8}{'legs':>6}{'cover':>8}{'cost':>8}{'fee':>7}{'edge':>9}")
    for k, lab in (("w2", "2-leg"), ("w3", "3-leg")):
        ms = acc[k]
        if not ms:
            continue
        cov = np.mean([m["cov"] for m in ms]); cost = np.mean([m["cost"] for m in ms])
        fee = np.mean([m["fee1"] for m in ms]); legs = np.mean([m["legs"] for m in ms])
        print(f"{lab:<8}{len(ms):>8}{legs:>6.1f}{cov:>8.0%}{cost:>8.3f}{fee:>7.3f}{cov - cost - fee:>+9.3f}")
    print()


def run():
    cfg = load_config()
    tab = build_table(cfg)
    diagnostic(tab)

    agg = {v: {"pnl": 0.0, "trades": 0, "cov": 0} for v in VARIANTS}
    test_days = 0
    percity = {}
    for s, days in tab.items():
        c2s = c2n = 0
        pc = {v: {"pnl": 0.0, "trades": 0, "cov": 0} for v in VARIANTS}
        pc_days = 0
        for ev, om in days:
            cov2 = (c2s / c2n) if c2n else None
            d0 = om[0]; m2_0 = d0["w2"]; m3_0 = d0.get("w3")
            if c2n >= WARMUP:
                test_days += 1; pc_days += 1
                if m2_0["cost"] < BASE_GATE:
                    _add(agg["B"], m2_0); _add(pc["B"], m2_0)
                if m2_0["cost"] + m2_0["fee1"] <= cov2:
                    _add(agg["S"], m2_0); _add(pc["S"], m2_0)
                if m3_0 and m3_0["cost"] < BASE_GATE:
                    _add(agg["F"], m3_0); _add(pc["F"], m3_0)
                mfw = _first_full(om)
                if mfw:
                    _add(agg["FW"], mfw); _add(pc["FW"], mfw)
                mh = _hybrid(om, cov2)
                if mh:
                    _add(agg["H"], mh); _add(pc["H"], mh)
                mh0 = m2_0 if (m2_0["cost"] + m2_0["fee1"] <= cov2) else (
                    m3_0 if (m3_0 and m3_0["cost"] < BASE_GATE) else None)
                if mh0:
                    _add(agg["H0"], mh0); _add(pc["H0"], mh0)
            if m2_0["cost"] < BASE_GATE:
                c2s += m2_0["cov"]; c2n += 1
        pc["days"] = pc_days
        percity[s] = pc

    print("=" * 92)
    print(f"WALK-FORWARD with FULL wing + hybrid  --  {test_days} post-warmup test days")
    print("=" * 92)
    lab = {"B": "B  2-wing 1PM @0.90 (status quo)", "S": "S  2-wing per-city select 1PM",
           "F": "F  FULL wing 1PM @0.90", "FW": "FW FULL wing windowed @0.90",
           "H": "H  hybrid (2-edge else full), windowed",
           "H0": "H0 hybrid (2-edge else full), 1PM only"}
    print(f"{'variant':<40}{'trades':>8}{'fire%':>7}{'cover':>7}{'PnL$':>10}{'edge/trade':>12}")
    for v in VARIANTS:
        a = agg[v]; tr = a["trades"]
        fire = tr / test_days if test_days else 0
        cov = a["cov"] / tr if tr else float("nan")
        ept = a["pnl"] / tr if tr else float("nan")
        print(f"{lab[v]:<40}{tr:>8}{fire:>7.0%}{cov:>7.0%}{a['pnl']:>+10.2f}{ept:>+12.3f}")

    print("\nDELTAS vs B (status quo):")
    for v in ["S", "F", "FW", "H", "H0"]:
        print(f"  {v:<3} - B   {agg[v]['pnl'] - agg['B']['pnl']:>+8.2f}")

    print("\nPER-CITY full-wing vs status quo (PnL$ with trades):")
    print(f"{'city':<15}{'nTest':>6}{'B':>13}{'F(full)':>13}{'H(hybrid)':>14}")
    for s, pc in sorted(percity.items(), key=lambda kv: -kv[1]["days"]):
        if pc["days"] == 0:
            continue
        def cell(v):
            return f"{pc[v]['pnl']:>+7.2f}({pc[v]['trades']:>2})"
        print(f"{CITY[s][0]:<15}{pc['days']:>6}{cell('B'):>13}{cell('F'):>13}{cell('H'):>14}")

    print("\nRead: if F/FW/H <= B, the full wing's higher coverage is eaten by its higher cost")
    print("(the drop_lower_ask premise) and the proposal does not beat trading the 2-wing once at 1PM.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
