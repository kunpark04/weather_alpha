"""Intraday convergence market-maker (MM) feasibility probe — strat_prod_2.

Reads a day's raw orderbook .jsonl snapshots (one file per bucket, as written by
scripts/orderbook_logger.py) and measures the quantities that decide whether a
two-sided market-making strategy can work on Kalshi daily-high temp buckets:

  1. SPREAD vs FEE      yes_ask - yes_bid (cents), against round-trip Kalshi fee at mid.
                        MM captures the spread; it must exceed round-trip fees + adverse sel.
  2. DEPTH              top-of-book sizes — is there size to quote against / get filled?
  3. ADVERSE SELECTION  |mid_t - mid_{t-1}| per step (cents). If price routinely jumps
                        more than the half-spread, a resting quote gets run over.
  4. FLOW               contracts traded during the contested window (delta of `vol`).
                        No counterparty flow => a maker quote never fills.
  5. CONVERGENCE        mid trajectory of the winning bucket — gradual drift (an edge a
                        nowcast-leaning MM could harvest) vs instantaneous jump (no edge).

Fee model: weather_alpha.fees.trade_fee_cents = ceil(7% * N * P * (1-P)) per execution
(symmetric — the code makes NO maker/taker distinction; that assumption is flagged in the
report). Per-contract per-side fee at price P = 7 * P * (1-P) cents; round trip ~2x.

Usage:
  python scripts/mm_feasibility_probe.py <dir-of-jsonl>          # one city/day
  python scripts/mm_feasibility_probe.py <dir1> <dir2> ...       # several
"""
from __future__ import annotations

import json
import math
import statistics as st
import sys
from datetime import datetime
from pathlib import Path


def fee_cents_per_contract(p: float) -> float:
    """Kalshi per-contract per-side fee in cents at price p (the un-ceil'd rate)."""
    p = max(0.0, min(1.0, p))
    return 7.0 * p * (1.0 - p)


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load_ticker(fp: Path) -> list[dict]:
    rows = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = r.get("ts")
        t = None
        if ts:
            try:
                t = datetime.fromisoformat(ts)
            except ValueError:
                t = None
        yb, ya = _f(r.get("yes_bid")), _f(r.get("yes_ask"))
        rows.append({
            "t": t,
            "yes_bid": yb, "yes_ask": ya,
            "yes_bid_size": _f(r.get("yes_bid_size")), "yes_ask_size": _f(r.get("yes_ask_size")),
            "vol": _f(r.get("vol")),
            "mid": ((yb + ya) / 2.0) if (yb is not None and ya is not None) else None,
            "spread": ((ya - yb)) if (yb is not None and ya is not None) else None,
        })
    return rows


def pctile(xs, q):
    xs = sorted(xs)
    if not xs:
        return float("nan")
    i = min(len(xs) - 1, max(0, int(round(q * (len(xs) - 1)))))
    return xs[i]


def analyze_ticker(name: str, rows: list[dict]) -> dict:
    ts = [r["t"] for r in rows if r["t"]]
    span_h = ((max(ts) - min(ts)).total_seconds() / 3600.0) if len(ts) >= 2 else 0.0

    # Contested window: genuinely two-sided AND uncertain (not pinned to 0/1).
    contested = [r for r in rows
                 if r["mid"] is not None and 0.05 <= r["mid"] <= 0.95
                 and r["yes_bid"] is not None and r["yes_bid"] > 0
                 and r["yes_ask"] is not None and r["yes_ask"] < 1.0
                 and r["spread"] is not None and r["spread"] > 0]

    spreads_c = [r["spread"] * 100 for r in contested]
    mids = [r["mid"] for r in contested]
    bid_sz = [r["yes_bid_size"] for r in contested if r["yes_bid_size"] is not None]
    ask_sz = [r["yes_ask_size"] for r in contested if r["yes_ask_size"] is not None]

    # Round-trip fee (cents/contract) at the median contested mid.
    med_mid = st.median(mids) if mids else float("nan")
    rt_fee_c = 2.0 * fee_cents_per_contract(med_mid) if mids else float("nan")
    med_spread_c = st.median(spreads_c) if spreads_c else float("nan")

    # Adverse selection: |mid change| per step within contested window (consecutive snaps).
    steps = []
    for a, b in zip(contested, contested[1:]):
        if a["mid"] is not None and b["mid"] is not None:
            steps.append(abs(b["mid"] - a["mid"]) * 100)

    # Flow during contested window (contracts traded = delta of cumulative vol).
    vols = [r["vol"] for r in contested if r["vol"] is not None]
    flow = (max(vols) - min(vols)) if len(vols) >= 2 else 0.0

    final_mid = next((r["mid"] for r in reversed(rows) if r["mid"] is not None), None)

    return {
        "name": name, "n": len(rows), "span_h": span_h,
        "n_contested": len(contested),
        "med_spread_c": med_spread_c,
        "p90_spread_c": pctile(spreads_c, 0.90),
        "med_mid": med_mid, "rt_fee_c": rt_fee_c,
        "net_capture_c": (med_spread_c - rt_fee_c) if spreads_c else float("nan"),
        "med_bid_sz": st.median(bid_sz) if bid_sz else 0.0,
        "med_ask_sz": st.median(ask_sz) if ask_sz else 0.0,
        "med_step_c": st.median(steps) if steps else float("nan"),
        "p90_step_c": pctile(steps, 0.90),
        "flow": flow,
        "final_mid": final_mid,
    }


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    dirs = [Path(a) for a in argv]
    rep = []
    for d in dirs:
        files = sorted(d.glob("*.jsonl"))
        for fp in files:
            rows = load_ticker(fp)
            rep.append(analyze_ticker(fp.stem, rows))

    hdr = (f"{'ticker':<28}{'n':>5}{'span_h':>7}{'cont':>6}"
           f"{'spr_c':>7}{'p90spr':>8}{'mid':>6}{'rtfee_c':>8}{'NET_c':>7}"
           f"{'bidsz':>9}{'asksz':>9}{'step_c':>8}{'p90step':>9}{'flow':>10}{'finmid':>8}")
    print(hdr)
    print("-" * len(hdr))
    for r in rep:
        print(f"{r['name']:<28}{r['n']:>5}{r['span_h']:>7.1f}{r['n_contested']:>6}"
              f"{r['med_spread_c']:>7.1f}{r['p90_spread_c']:>8.1f}{r['med_mid']:>6.2f}"
              f"{r['rt_fee_c']:>8.2f}{r['net_capture_c']:>7.1f}"
              f"{r['med_bid_sz']:>9.0f}{r['med_ask_sz']:>9.0f}"
              f"{r['med_step_c']:>8.2f}{r['p90_step_c']:>9.2f}{r['flow']:>10.0f}{r['final_mid'] if r['final_mid'] is not None else float('nan'):>8.2f}")
    print()
    print("Legend: cont=contested snaps (0.05<mid<0.95, two-sided); spr_c=median spread (cents);")
    print("  rtfee_c=round-trip fee/contract at median mid; NET_c=spr_c-rtfee_c (>0 needed BEFORE adverse sel);")
    print("  step_c=median |mid move|/snapshot (cents, ~adverse selection); flow=contracts traded in window.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
