"""Probe: is the historical KXHIGHCHI trade tape DENSE enough to reconstruct a 1 PM book?

Settled events going back to 2021 (probe_kalshi_history_depth) only help if the PRICES exist:
the wing needs >=2 (ideally the full 6) buckets to have traded BEFORE 1 PM local that day. Early
markets may be illiquid. This samples KXHIGHCHI events across 2021->2026 and, per sample day,
reports how reconstructable the 1 PM book is.

Per sampled event:
  n_mkts        = bucket markets in the event
  trades_total  = trades across all buckets
  pre1pm_tot    = trades created at/before 13:00 America/Chicago on the strike date
  buckets_pre1  = distinct buckets with >=1 pre-1PM trade  (>=2 needed to form a wing; 6 = full book)
  ask_sum_top2  = sum of the 2 highest last-pre-1PM yes prices (sanity: a plausible wing cost)

Public API, no auth. Read-only.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.probe_kalshi_history_depth import ev_date, get

CT = ZoneInfo("America/Chicago")


def all_events(series="KXHIGHCHI"):
    cur, out, pages = None, [], 0
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 200}
        if cur:
            p["cursor"] = cur
        d = get("/events", p)
        for e in d.get("events") or []:
            dt = ev_date(e)
            if dt is not None:
                out.append((e.get("event_ticker", ""), dt))
        cur = d.get("cursor") or None
        pages += 1
        if not cur or pages > 80:
            break
    return sorted(out, key=lambda x: x[1])


def trades(ticker, max_pages=60):
    cur, out, pages = None, [], 0
    while True:
        p = {"ticker": ticker, "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = get("/markets/trades", p)
        ts = d.get("trades") or []
        out.extend(ts)
        cur = d.get("cursor") or None
        pages += 1
        if not cur or not ts or pages >= max_pages:
            break
    return out


def analyze(event_ticker, strike_date):
    anchor = pd.Timestamp(strike_date.year, strike_date.month, strike_date.day, 13, tz=CT).tz_convert("UTC")
    mkts = (get("/markets", {"event_ticker": event_ticker, "limit": 100}).get("markets") or [])
    trades_total = pre1 = 0
    last_pre = {}                      # ticker -> last pre-1PM yes price (cents)
    for m in mkts:
        tkr = m.get("ticker")
        if not tkr:
            continue
        rows = trades(tkr)
        trades_total += len(rows)
        best_ts = None
        for t in rows:
            ts = pd.to_datetime(t.get("created_time"), utc=True, errors="coerce")
            if ts is pd.NaT or ts > anchor:
                continue
            pre1 += 1
            if best_ts is None or ts > best_ts:
                best_ts = ts
                yc = t.get("yes_price_dollars")
                try:
                    last_pre[tkr] = int(round(float(yc) * 100)) if yc is not None else t.get("yes_price")
                except (TypeError, ValueError):
                    pass
    top2 = sorted(last_pre.values(), reverse=True)[:2]
    ask_sum_top2 = sum(v / 100.0 for v in top2) if len(top2) == 2 else float("nan")
    return {"n_mkts": len(mkts), "trades_total": trades_total, "pre1pm_tot": pre1,
            "buckets_pre1": len(last_pre), "ask_sum_top2": ask_sum_top2}


def main():
    evs = all_events("KXHIGHCHI")
    if not evs:
        print("no events"); return 1
    print(f"KXHIGHCHI settled events: {len(evs)}  ({evs[0][1].date()} -> {evs[-1][1].date()})")

    # Sample ~one event per ~6 months across the full span.
    targets = pd.date_range(evs[0][1], evs[-1][1], periods=11)
    picked, used = [], set()
    for tgt in targets:
        best = min(evs, key=lambda e: abs((e[1] - tgt).days))
        if best[0] not in used:
            used.add(best[0]); picked.append(best)

    print(f"\n{'strike_date':<13}{'event_ticker':<22}{'mkts':>5}{'trades':>8}{'pre1pm':>8}"
          f"{'buckets<=1PM':>13}{'top2_asksum':>13}")
    print("-" * 82)
    for et, dt in picked:
        r = analyze(et, dt)
        asum = f"{r['ask_sum_top2']:.2f}" if r['ask_sum_top2'] == r['ask_sum_top2'] else "  --"
        flag = "" if r["buckets_pre1"] >= 2 else "   <-- NOT reconstructable"
        print(f"{dt.date()!s:<13}{et:<22}{r['n_mkts']:>5}{r['trades_total']:>8}{r['pre1pm_tot']:>8}"
              f"{r['buckets_pre1']:>13}{asum:>13}{flag}")

    print("\nbuckets<=1PM >= 2  => a wing is formable that day (6 = full book).  Dense across the span")
    print("=> the whole ~4.8yr Chicago history is backtestable; thin early => usable window is shorter.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
