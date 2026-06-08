"""Wing coverage (c) vs cost (S) vs edge by ANCHOR HOUR -- to SEE why 1 PM is the knee.

For each anchor hour h (6 AM..4 PM local), reconstruct each city-day's book at h:00 from the trade
tape and run the production 2-leg wing UNGATED (max_ask_sum=0.999) on EVERY day -- a FIXED
population -- recording coverage c (winner in the re-centered wing), mean cost S (sum of the 2 leg
asks), fee, and edge = c - S - fee (per $1 payout). Pooled across all cities + Chicago alone.

The knee: c rises through the morning as information arrives; S rises through the afternoon as the
market resolves; the cushion c - S (the edge) is widest where the two are farthest apart. Ungated +
fixed population => this is a STRUCTURAL curve, not the survivorship/contaminated per-offset View A.
The late-hour (15-16) uptick is partly the daily high already being realized (observation, not a
tradeable entry) -- the genuine decision knee is the midday peak.
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
from weather_alpha.strategy import run_wing_strategy

HOURS = range(6, 17)                       # 6 AM .. 4 PM local
FEE1 = lambda a: 0.07 * a * (1 - a)         # per-$1-payout fee


def wing_cs(day, winner, anchor_utc, cfg, ev) -> dict | None:
    contracts = _snapshot(day, anchor_utc)
    if len(contracts) < 2:
        return None
    out = run_wing_strategy(
        cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
        require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
        flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
        drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.999)
    if not out.targets:
        return None
    legs, cov = [], 0
    for tgt in out.targets:
        c = next((x for x in contracts if x.ticker == tgt.ticker), None)
        if c is None:
            continue
        legs.append(c.yes_ask)
        cov = cov or int(tgt.ticker == winner)
    if not legs:
        return None
    return {"cov": cov, "S": float(sum(legs)), "fee": float(sum(FEE1(a) for a in legs))}


def collect() -> pd.DataFrame:
    cfg = load_config()
    rows = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        tz = ZoneInfo(CITY[s][1])
        for ev, day in df.groupby("event_date"):
            w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
            if len(w) != 1:
                continue
            winner = w[0]
            for h in HOURS:
                anchor = pd.Timestamp(ev.year, ev.month, ev.day, h, tz=tz).tz_convert("UTC")
                r = wing_cs(day, winner, anchor, cfg, ev)
                if r:
                    rows.append({"series": s, "hour": h, **r})
    return pd.DataFrame(rows)


def show(d: pd.DataFrame, title: str):
    print(f"\n{title}")
    print(f"{'hour':>5}{'n':>6}{'cover c':>9}{'cost S':>9}{'gap c-S':>10}{'edge(-fee)':>12}")
    best_h, best_e = None, -9.0
    for h in HOURS:
        g = d[d.hour == h]
        if g.empty:
            continue
        c, S, f = g["cov"].mean(), g["S"].mean(), g["fee"].mean()
        e = c - S - f
        if e > best_e:
            best_e, best_h = e, h
        star = " <-- 1 PM" if h == 13 else ""
        print(f"{h:>5}{len(g):>6}{c:>9.1%}{S:>9.3f}{c - S:>+10.3f}{e:>+12.3f}{star}")
    print(f"  peak edge at hour {best_h} (edge {best_e:+.3f})")


def main():
    d = collect()
    if d.empty:
        print("no data"); return 1
    show(d, "POOLED across all cities -- 2-leg wing, UNGATED, by anchor hour (local)")
    show(d[d.series == "KXHIGHCHI"], "CHICAGO only (the live market)")
    print("\nc = coverage (winner in the re-centered wing); S = mean cost (sum of 2 leg asks);")
    print("edge = c - S - fee per $1.  Ungated + fixed population => structural, not survivorship.")
    print("The midday peak is the knee (c high, S not yet caught up). The 15-16h rise is partly the")
    print("high being realized = observation, not a tradeable entry. Early hours have thin books (low n).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
