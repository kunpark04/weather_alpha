"""Liquidity check for the model-free `market_wing` bot (HANDOFF §7 `P2`).

The production wing places a TINY order — flat $2.50 split equal-payout across ~3 legs
(modal Kalshi bucket + adjacents), i.e. roughly **~2 contracts per leg, ~6 total** — right
after each city's **1 PM local anchor** (CDT season → 18:00 UTC), inside a 60-min window.

Two liquidity questions:
  (A) Is there enough VOLUME near the anchor, in the mid-priced buckets the wing trades, to
      absorb ~2 contracts/leg?  -> answerable NOW from the trade tape (this script).
  (B) Does it fill at the assumed price (mid+1c / the ask)?  -> needs the resting bid/ask
      DEPTH ladder, which `scripts/orderbook_logger.py` is only now accumulating. Deferred.

This script answers (A) from `data/kalshi_history.parquet` (a trade tape: one row per print,
`yes_price_cents` + `count` contracts + `taker_side`; it has NO resting depth, so it bounds
*realized* liquidity, not quote depth). Verdict framing: compare per-leg window volume to the
~2-contract-per-leg need.

Usage:
  python scripts/liquidity_check.py                 # 0..60 min post-anchor, mid band 15-85c
  python scripts/liquidity_check.py --window 60 --pmin 15 --pmax 85
"""
from __future__ import annotations

import argparse
import numpy as np
import pandas as pd

ANCHOR_UTC_HOUR = 18  # 1 PM America/Chicago in CDT (Mar-Nov) — the kalshi_history window is all CDT


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="market_wing liquidity check (trade-tape half of P2)")
    ap.add_argument("--history", default="data/kalshi_history.parquet")
    ap.add_argument("--window", type=int, default=60, help="post-anchor minutes the bot trades in")
    ap.add_argument("--pmin", type=int, default=15, help="min yes_price (c) for a 'tradeable' mid bucket")
    ap.add_argument("--pmax", type=int, default=85, help="max yes_price (c) for a 'tradeable' mid bucket")
    ap.add_argument("--per-leg", type=int, default=2, help="contracts the wing needs per leg")
    args = ap.parse_args(argv)

    df = pd.read_parquet(args.history)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    ed = pd.to_datetime(df["event_date"]).dt.tz_localize("UTC")
    anchor = ed + pd.Timedelta(hours=ANCHOR_UTC_HOUR)
    df["off_min"] = (df["timestamp"] - anchor).dt.total_seconds() / 60.0

    n_events = df["event_ticker"].nunique()
    print(f"=== dataset === {len(df):,} trade prints | {n_events} events | "
          f"{df['event_date'].min().date()} -> {df['event_date'].max().date()} | "
          f"{df['count'].sum():,.0f} contracts total")

    # --- time profile: where does volume sit relative to the 1 PM anchor? ---
    bins = [-10**9, -120, -60, 0, 60, 120, 240, 10**9]
    labels = ["<-2h", "-2h..-1h", "-1h..0", "0..+1h (BOT)", "+1h..+2h", "+2h..+4h", ">+4h"]
    prof = df.groupby(pd.cut(df["off_min"], bins=bins, labels=labels), observed=False)["count"].sum()
    tot = prof.sum()
    print("\n=== contracts by offset from 1 PM anchor (all buckets) ===")
    for lab, v in prof.items():
        print(f"  {lab:<14} {v:>12,.0f}  ({100*v/tot:5.1f}%)")

    # --- the bot's actual trading slice: post-anchor window, mid-priced buckets ---
    win = df[(df["off_min"] >= 0) & (df["off_min"] <= args.window)
             & (df["yes_price_cents"] >= args.pmin) & (df["yes_price_cents"] <= args.pmax)].copy()
    print(f"\n=== bot slice: 0..+{args.window}min, yes {args.pmin}-{args.pmax}c "
          f"({len(win):,} prints, {win['count'].sum():,.0f} contracts) ===")

    # per (event, bucket) = a wing LEG: realized volume in the window
    leg = win.groupby(["event_ticker", "ticker"])["count"].sum()
    # per event: total tradeable volume + distinct mid buckets available
    ev = win.groupby("event_ticker").agg(vol=("count", "sum"),
                                         buckets=("ticker", "nunique"),
                                         prints=("count", "size"))
    events_with_window = ev.shape[0]
    print(f"events with ANY mid-bucket trade in the window: {events_with_window} / {n_events} "
          f"({100*events_with_window/n_events:.0f}%)")

    def pct(s, ps=(0, 10, 25, 50, 75, 90)):
        return "  ".join(f"p{p}={np.percentile(s, p):.0f}" for p in ps) if len(s) else "n/a"

    print("\nper-EVENT window volume (contracts):   " + pct(ev["vol"].values))
    print("per-EVENT mid buckets traded:          " + pct(ev["buckets"].values))
    print("per-LEG window volume (event,bucket):  " + pct(leg.values))
    print("per-PRINT trade size (contracts):      " + pct(win["count"].values))

    # --- verdict vs the ~per-leg need ---
    need = args.per_leg
    leg_ok = (leg >= need).mean() * 100 if len(leg) else float("nan")
    ev_ok6 = (ev["vol"] >= need * 3).mean() * 100 if len(ev) else float("nan")
    print(f"\n=== verdict (need ~{need} contracts/leg, ~{need*3} total) ===")
    print(f"  legs with >= {need} contracts traded in-window:   {leg_ok:5.1f}% of (event,bucket) cells")
    print(f"  events with >= {need*3} tradeable contracts in-window: {ev_ok6:5.1f}%")
    print(f"  median per-leg in-window volume:                 {np.median(leg.values):.0f} contracts"
          if len(leg) else "  (no legs)")
    print("\nNOTE: trade tape bounds REALIZED volume, not resting quote depth. The mid+1c / ask "
          "fill-PRICE question (P2 part B) needs the orderbook depth ladders now accruing via "
          "scripts/orderbook_logger.py — re-run a depth-based check once ~2 weeks have logged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
