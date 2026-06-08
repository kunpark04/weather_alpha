"""Intraday-decay backtest: does the production 2-leg wing's COVERAGE and EDGE survive past the
1 PM anchor? For each city-day it reconstructs the book at 1 PM + {0,15,30,60,120,180} min from the
trade tape (_snapshot = last trade <= t per ticker, +1c ask proxy) and re-runs the SAME production
wing (market_wing, drop_lower_ask, gate<=0.90, assumed 0.92) at each offset.

Answers 'can the bot keep monitoring after 1 PM for more entries?':
  View A  per-offset FIRING population: coverage + edge you get if you enter at that offset.
  View B  MARGINAL late-only entries (fire at offset t>0 but did NOT fire at 1 PM) -- the EXACT
          population the 'keep monitoring' proposal ADDS. If its edge <= 0 the feature is net-bad.
  View C  Chicago (the live market) alone, per offset.
  View D  group means edge>0-at-1PM cities vs the rest, at 1 PM vs +60 min.

Mechanism under test (adverse selection): a wing's ask sum IS the market's P(winner inside it); a
wing still cheap enough to fire LATE is usually cheap because the winner has drifted OUT of it ->
late fills are selected toward misses. CAVEAT: the last-trade proxy only updates a ticker when it
trades, so afternoon sharpening here is a LOWER BOUND (a true orderbook/WS feed sharpens faster) ->
any decay measured is conservative; flat coverage here does NOT prove flat coverage live.
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

OFFSETS = [0, 15, 30, 60, 120, 180]                       # minutes past the 1 PM local anchor
LABEL = {0: "1:00", 15: "1:15", 30: "1:30", 60: "2:00", 120: "3:00", 180: "4:00"}
FEE1 = lambda a: 0.07 * a * (1 - a)                       # per-$1-payout fee (edge convention)


def wing_at(day, winner, anchor_utc, cfg, ev) -> dict | None:
    """Run the production wing on the book reconstructed at anchor_utc. None => no book (<2 legs)."""
    contracts = _snapshot(day, anchor_utc)
    if len(contracts) < 2:
        return None
    out = run_wing_strategy(
        cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
        require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
        flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
        drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.90)
    if not out.targets:
        return {"fired": False, "covered": 0, "cost": np.nan, "fee1": np.nan}
    leg_asks, covered = [], 0
    for tgt in out.targets:
        c = next((x for x in contracts if x.ticker == tgt.ticker), None)
        if c is None:
            continue
        leg_asks.append(c.yes_ask)
        covered = covered or int(tgt.ticker == winner)
    return {"fired": True, "covered": covered,
            "cost": float(sum(leg_asks)), "fee1": float(sum(FEE1(a) for a in leg_asks))}


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
            base = pd.Timestamp(ev.year, ev.month, ev.day, 13, tz=tz).tz_convert("UTC")
            for off in OFFSETS:
                r = wing_at(day, winner, base + pd.Timedelta(minutes=off), cfg, ev)
                if r is None:
                    continue                              # no book at this offset
                rows.append({"city": CITY[s][0], "series": s, "date": ev, "offset": off, **r})
    return pd.DataFrame(rows)


def _agg(g: pd.DataFrame) -> dict:
    f = g[g["fired"]]
    if f.empty:
        return {"n": 0, "cov": np.nan, "cost": np.nan, "fee": np.nan, "edge": np.nan}
    cov = float(f["covered"].mean()); cost = float(f["cost"].mean()); fee = float(f["fee1"].mean())
    return {"n": len(f), "cov": cov, "cost": cost, "fee": fee, "edge": cov - cost - fee}


def per_offset(g: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame([{"offset": o, **_agg(g[g["offset"] == o])} for o in OFFSETS])


def main():
    rec = collect()
    if rec.empty:
        print("no data"); return 1

    # fired@0 per (series,date) to isolate the marginal late-only entries (View B)
    base0 = (rec[rec["offset"] == 0].set_index(["series", "date"])["fired"]
             .rename("fired0"))
    rec = rec.join(base0, on=["series", "date"])
    rec["had_book0"] = rec["fired0"].notna()

    def show(tab, title, base_edge=None):
        print(f"\n{title}")
        print(f"{'time':>6}{'nFired':>8}{'cover':>8}{'cost':>8}{'fee':>7}{'edge':>9}{'dEdge':>9}")
        e0 = tab.loc[tab.offset == 0, "edge"].iloc[0] if base_edge is None else base_edge
        for _, r in tab.iterrows():
            de = "" if np.isnan(r["edge"]) else f"{r['edge'] - e0:>+9.3f}"
            cov = "  n/a" if np.isnan(r["cov"]) else f"{r['cov']:>8.0%}"
            cost = "      " if np.isnan(r["cost"]) else f"{r['cost']:>8.3f}"
            fee = "     " if np.isnan(r["fee"]) else f"{r['fee']:>7.3f}"
            edge = "      " if np.isnan(r["edge"]) else f"{r['edge']:>+9.3f}"
            print(f"{LABEL[int(r['offset'])]:>6}{int(r['n']):>8}{cov}{cost}{fee}{edge}{de}")

    print("=" * 64)
    print("INTRADAY DECAY of the production 2-leg wing (pooled across all cities)")
    print("  per-$1-payout cost/fee/edge; entering at each offset past the 1 PM anchor")
    print("=" * 64)
    A = per_offset(rec)
    show(A, "View A -- FIRING population at each offset (what you'd get entering then):")

    # View B: marginal entries that did NOT fire at 1 PM but DO fire at offset t (the feature's adds)
    late = rec[(rec["offset"] > 0) & rec["fired"] & rec["had_book0"] & (~rec["fired0"].fillna(True))]
    B = pd.DataFrame([{"offset": o, **_agg(late[late["offset"] == o])} for o in OFFSETS if o > 0])
    print("\nView B -- MARGINAL late-only entries (fire at t but NOT at 1 PM = what the proposal ADDS):")
    print(f"{'time':>6}{'nAdds':>8}{'cover':>8}{'cost':>8}{'fee':>7}{'edge':>9}")
    for _, r in B.iterrows():
        if r["n"] == 0:
            print(f"{LABEL[int(r['offset'])]:>6}{0:>8}{'  n/a':>8}"); continue
        print(f"{LABEL[int(r['offset'])]:>6}{int(r['n']):>8}{r['cov']:>8.0%}{r['cost']:>8.3f}"
              f"{r['fee']:>7.3f}{r['edge']:>+9.3f}")

    # View C: Chicago alone (the live market)
    chi = rec[rec["series"] == "KXHIGHCHI"]
    if not chi.empty:
        show(per_offset(chi), "View C -- CHICAGO only (the live market):")

    # View D: group by edge>0 at 1 PM, compare 1 PM vs +60 min
    e0_by_city = {s: _agg(g[g.offset == 0])["edge"]
                  for s, g in rec.groupby("series")}
    pos = [s for s, e in e0_by_city.items() if e is not None and e > 0]
    rec["grp"] = np.where(rec["series"].isin(pos), "edge>0 @1PM", "edge<=0 @1PM")
    print("\nView D -- group means, 1 PM vs +60 min  (edge per $1):")
    print(f"{'group':<16}{'cov@1PM':>9}{'edge@1PM':>10}{'cov@2PM':>9}{'edge@2PM':>10}{'dEdge':>9}")
    for grp, g in rec.groupby("grp"):
        a0, a60 = _agg(g[g.offset == 0]), _agg(g[g.offset == 60])
        print(f"{grp:<16}{a0['cov']:>9.0%}{a0['edge']:>+10.3f}{a60['cov']:>9.0%}"
              f"{a60['edge']:>+10.3f}{a60['edge'] - a0['edge']:>+9.3f}")

    print("\nReading it: View A edge falling as time grows = the firing pool gets adversely selected.")
    print("View B is decisive -- it is exactly the trades the 'keep monitoring' feature would add.")
    print("(last-trade proxy => afternoon sharpening understated; live decay is >= what is shown.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
