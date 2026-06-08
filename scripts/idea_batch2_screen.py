"""idea_batch2_screen.py -- batch #2 of model-free edge screens (strat_prod_2 repoint).

Reuses the batch-#1 helpers (events/wing_fire/_edge). MODEL-FREE, deep tape, IS/OOS 80/20.

  D. CROSS-CITY CONFIDENCE SYNCHRONY: does Chicago's wing edge depend on a market-WIDE confidence
     regime? Per date, take the mean modal confidence (max yes_ask) of the OTHER 19 cities at their
     1 PM anchors; split Chicago fires into terciles of that signal and compare edge. (Hindsight
     UPPER BOUND on tradeability: PT-city anchors post-date Chicago's 18:00Z anchor, so a live gate
     could only use earlier-anchoring cities -- but if there's no edge even with full hindsight, dead.)

  E. LONG-CONFIRM: back the single market-modal bucket (the favorite) at each anchor hour 11..16 and
     hold to settlement -- does buying the favorite get MORE +EV later in the day as the running max
     locks in? Complements the dead ratchet SHORT (which sold dead buckets). Edge = win - ask - fee,
     per $1, by hour. CAVEAT (L14): late-hour last-trade prices go stale -> a backfill read is
     optimistic on cost late; treat the late-hour numbers as an upper bound, not live-realizable.
"""
from __future__ import annotations

import sys
from zoneinfo import ZoneInfo
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import _snapshot
from scripts.idea_batch_screen import SPLIT, _edge, events, wing_fire
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config
from weather_alpha.fees import trade_fee_cents


# ---- D. cross-city confidence synchrony -----------------------------------------------------------
def screen_cross_city(cfg, focus="KXHIGHCHI"):
    all_series = [s for s in CITY if (BACKFILL / s).is_dir()]
    conf: dict = {}                                   # date -> [other-city modal max_ask]
    for s in all_series:
        if s == focus:
            continue
        for ev, contracts, _w in events(s):
            live = [c for c in contracts if c.is_live]
            if live:
                conf.setdefault(ev, []).append(max(c.yes_ask for c in live))
    cross = {d: float(np.mean(v)) for d, v in conf.items() if v}

    fired = [r for r in (wing_fire(cfg, ev, c, w) for ev, c, w in events(focus)) if r]
    for r in fired:
        r["cross"] = cross.get(r["date"], float("nan"))
    fired = [r for r in fired if r["cross"] == r["cross"]]
    if len(fired) < 30:
        print(f"\n[D] CROSS-CITY -- too few matched fires ({len(fired)})"); return

    xs = sorted(r["cross"] for r in fired)
    q1, q2 = xs[len(xs) // 3], xs[2 * len(xs) // 3]
    dates = sorted({r["date"] for r in fired})
    cut = dates[int(len(dates) * SPLIT)]
    print(f"\n[D] CROSS-CITY CONFIDENCE SYNCHRONY -- {CITY[focus][0]} wing edge by OTHER-cities' mean modal conf")
    print(f"  {'tercile':<8}{'range':<14}{'N':>5}{'WR%':>7}{'IS edge':>9}{'OOS edge':>10}")
    print("  " + "-" * 53)
    for name, lo, hi in (("low", -1, q1), ("mid", q1, q2), ("high", q2, 2)):
        grp = [r for r in fired if lo <= r["cross"] < hi] if name != "high" else [r for r in fired if r["cross"] >= lo]
        if not grp:
            continue
        e_is, _ = _edge([r for r in grp if r["date"] < cut])
        e_oos, _ = _edge([r for r in grp if r["date"] >= cut])
        wr = np.mean([r["cov"] for r in grp]) * 100
        eis = f"{e_is:>+8.1f}" if e_is == e_is else f"{'-':>8}"
        eo = f"{e_oos:>+9.1f}" if e_oos == e_oos else f"{'-':>9}"
        rng = f"{(lo if name!='low' else xs[0]):.2f}-{(hi if name!='high' else xs[-1]):.2f}"
        print(f"  {name:<8}{rng:<14}{len(grp):>5}{wr:>6.0f}{eis:>9}{eo:>10}")
    print("  A spatial edge => Chicago's wing edge rises with OTHER cities' confidence, persisting OOS.")


# ---- E. long-confirm (back the modal by hour) -----------------------------------------------------
def screen_long_confirm(focus="KXHIGHCHI", hours=range(11, 17)):
    df = load_city(focus)
    tz = ZoneInfo(CITY[focus][1])
    recs = []
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        winner = w[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        for h in hours:
            anchor = pd.Timestamp(ev.year, ev.month, ev.day, h, tz=tz).tz_convert("UTC")
            cs = _snapshot(day, anchor)
            if len(cs) < 2:
                continue
            modal = max(cs, key=lambda c: c.yes_ask)
            net = (100 if modal.ticker == winner else 0) - int(round(modal.yes_ask * 100)) \
                - trade_fee_cents(modal.yes_ask, 1)
            recs.append({"hour": h, "date": ev, "ask": modal.yes_ask,
                         "won": int(modal.ticker == winner), "net_c": net})
    d = pd.DataFrame(recs)
    if d.empty:
        print("\n[E] LONG-CONFIRM -- no data"); return
    dates = sorted(d["date"].unique())
    cut = dates[int(len(dates) * SPLIT)]
    print(f"\n[E] LONG-CONFIRM -- {CITY[focus][0]} back-the-modal (single leg) edge by anchor hour")
    print(f"  {'hour':>5}{'N':>6}{'mean_ask':>9}{'win%':>7}{'IS edge':>9}{'OOS edge':>10}")
    print("  " + "-" * 46)
    for h in hours:
        hd = d[d["hour"] == h]
        if hd.empty:
            continue
        e_is = hd[hd["date"] < cut]["net_c"].mean()
        e_oos = hd[hd["date"] >= cut]["net_c"].mean()
        eis = f"{e_is:>+8.1f}" if e_is == e_is else f"{'-':>8}"
        eo = f"{e_oos:>+9.1f}" if e_oos == e_oos else f"{'-':>9}"
        print(f"  {h:>5}{len(hd):>6}{hd['ask'].mean():>9.2f}{hd['won'].mean()*100:>6.0f}{eis:>9}{eo:>10}")
    print("  edge = win - ask - fee (cents per $1, single modal contract). Long-confirm would show edge")
    print("  rising toward/through 0 at later hours; CAVEAT L14: late last-trade prices are STALE")
    print("  (optimistic cost) so late-hour edge is an upper bound, not live-realizable.")


def main(argv) -> int:
    focus = argv[0] if argv else "KXHIGHCHI"
    cfg = load_config()
    print("=" * 64)
    print(f"MODEL-FREE IDEA BATCH #2  (deep tape, IS/OOS 80/20)  focus={focus}")
    print("=" * 64)
    screen_cross_city(cfg, focus)
    screen_long_confirm(focus)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
