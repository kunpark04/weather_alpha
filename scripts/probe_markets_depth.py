"""Decisive probe: how far back are MARKETS (not just events) and CANDLESTICKS retrievable?

probe_old_markets showed /markets?event_ticker=<old> returns 0 even for a 6-month-old event.
Two surfaces still untested, each with its own retention:
  1. Full cursor-pagination of /markets?series_ticker=...&status=settled (NO time filter) ->
     find the OLDEST settled market actually returned. This is the true markets-layer depth.
  2. Candlesticks (/series/{s}/markets/{ticker}/candlesticks) -- a per-market price-history
     endpoint that frequently outlives the /markets listing. Test on the newest market, then on
     an OLD market ticker if pagination surfaces one.

Tries both KXHIGHCHI (current series) and HIGHCHI (legacy series name). Public API, no auth.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.probe_kalshi_history_depth import get


def page_settled_markets(series, max_pages=80):
    """Cursor-paginate ALL settled markets for a series; return list of (ticker, close_dt)."""
    cur, out, pages = None, [], 0
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = get("/markets", p)
        if d.get("_status") == 404 or "_error" in d:
            print(f"    {series}: {d}")
            break
        mk = d.get("markets") or []
        for m in mk:
            ct = m.get("close_time") or m.get("expiration_time") or m.get("latest_expiration_time")
            out.append((m.get("ticker", ""), pd.to_datetime(ct, utc=True, errors="coerce")))
        cur = d.get("cursor") or None
        pages += 1
        if not cur or not mk or pages >= max_pages:
            break
    return out, pages


def candles(series, ticker, around_dt, period=60):
    """n candlesticks for ticker in a 3-day window around around_dt; -1 on fail."""
    if around_dt is pd.NaT or around_dt is None:
        around_dt = pd.Timestamp.utcnow()
    start = int((around_dt - pd.Timedelta(days=1)).timestamp())
    end = int((around_dt + pd.Timedelta(days=2)).timestamp())
    d = get(f"/series/{series}/markets/{ticker}/candlesticks",
            {"start_ts": start, "end_ts": end, "period_interval": period})
    if d.get("_status") == 404 or "_error" in d:
        return -1, d.get("_status", d.get("_error"))
    return len(d.get("candlesticks") or []), "ok"


def main():
    for series in ("KXHIGHCHI", "HIGHCHI"):
        print("=" * 86)
        print(f"SERIES {series}: cursor-paginate ALL settled markets (no time filter)")
        print("=" * 86)
        mk, pages = page_settled_markets(series)
        mk = [(t, d) for t, d in mk if t]
        if not mk:
            print("  -> 0 settled markets returned.\n")
            continue
        dated = [(t, d) for t, d in mk if d is not pd.NaT]
        dated.sort(key=lambda x: x[1])
        oldest = dated[0] if dated else (mk[0][0], "n/a")
        newest = dated[-1] if dated else (mk[-1][0], "n/a")
        print(f"  {len(mk)} settled markets over {pages} pages")
        print(f"  oldest close: {oldest[1]}  ({oldest[0]})")
        print(f"  newest close: {newest[1]}  ({newest[0]})")

        # candlesticks on newest + oldest returned market
        for label, (tkr, dt) in (("newest", newest), ("oldest", oldest)):
            n, status = candles(series, tkr, dt if dt != "n/a" else None)
            print(f"  candlesticks[{label}] {tkr}: n={n} ({status})")

        # also: candlesticks on a CONSTRUCTED old ticker (does the price endpoint outlive listing?)
        print()

    # Direct candlestick reach test on a known old market-ticker shape, if /markets won't list it.
    print("=" * 86)
    print("Candlestick reach on CONSTRUCTED old tickers (if listing is windowed but prices persist)")
    print("=" * 86)
    # Old Chicago markets were e.g. HIGHCHI-23JUL20-B<strike>; we don't know strikes, so try a few.
    for tk in ["HIGHCHI-23JUL20-T86", "HIGHCHI-23JUL20-B85.5", "KXHIGHCHI-25DEC11-B40.5",
               "KXHIGHCHI-25DEC11-T44"]:
        ser = "HIGHCHI" if tk.startswith("HIGHCHI") else "KXHIGHCHI"
        n, status = candles(ser, tk, pd.Timestamp("2023-07-20" if "23JUL" in tk else "2025-12-11", tz="UTC"))
        print(f"  {tk:<28} n={n} ({status})")

    print("\nVERDICT: oldest retrievable settled-market close date = the REAL backtest depth ceiling.")
    print("If it's ~67d back -> Kalshi windows the market/trade layer (events persist, prices don't).")
    print("If it reaches 2021/2023 -> deep history is pullable and the backtest can be extended.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
