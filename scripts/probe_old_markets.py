"""Probe: can OLD settled markets + their trade tape actually be RETRIEVED?

probe_trade_density showed /markets?event_ticker=<old> returns 0 markets (it only exposes
recently-active markets -> that's why our backfill stopped at 67 days). Before claiming the
deep history is usable, test whether the old markets/trades are retrievable another way.

For two out-of-window events (one 2023 'HIGHCHI-*', one late-2025 'KXHIGHCHI-*'), try:
  A. /markets?event_ticker=...&status=settled                  (explicit settled status)
  B. /markets?event_ticker=...   (baseline, no status)
  C. /events/{event_ticker}?with_nested_markets=true            (event detail w/ nested markets)
  D. /markets?series_ticker=KXHIGHCHI&status=settled  paged by max_close_ts back in time
Then for any market ticker found: count trades + pre-1PM trades, and try candlesticks
(/series/KXHIGHCHI/markets/{ticker}/candlesticks) as an alt price source.

Public API, no auth. Read-only. Decides whether ~4.8yr Chicago history is pullable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.probe_kalshi_history_depth import get

CT = ZoneInfo("America/Chicago")
TEST_EVENTS = [("HIGHCHI-23JUL20", "2023-07-20"), ("KXHIGHCHI-25DEC11", "2025-12-11")]


def count_trades(ticker, anchor_utc, max_pages=60):
    cur, n, pre, pages = None, 0, 0, 0
    while True:
        p = {"ticker": ticker, "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = get("/markets/trades", p)
        ts = d.get("trades") or []
        for t in ts:
            n += 1
            tt = pd.to_datetime(t.get("created_time"), utc=True, errors="coerce")
            if tt is not pd.NaT and tt <= anchor_utc:
                pre += 1
        cur = d.get("cursor") or None
        pages += 1
        if not cur or not ts or pages >= max_pages:
            break
    return n, pre


def candle_count(series, ticker, strike_date):
    """Try candlesticks for ticker over the strike day. Returns n candles or -1 on fail."""
    start = pd.Timestamp(strike_date, tz=CT).tz_convert("UTC")
    end = start + pd.Timedelta(days=2)
    d = get(f"/series/{series}/markets/{ticker}/candlesticks",
            {"start_ts": int(start.timestamp()), "end_ts": int(end.timestamp()),
             "period_interval": 60})
    if d.get("_status") == 404 or "_error" in d:
        return -1
    return len(d.get("candlesticks") or [])


def main():
    for et, ds in TEST_EVENTS:
        strike = pd.Timestamp(ds)
        anchor = pd.Timestamp(strike.year, strike.month, strike.day, 13, tz=CT).tz_convert("UTC")
        print("=" * 86)
        print(f"EVENT {et}  (strike {ds}, 1PM CT anchor = {anchor})")
        print("=" * 86)

        a = get("/markets", {"event_ticker": et, "status": "settled", "limit": 100})
        ma = a.get("markets") or []
        print(f"  A. /markets?event_ticker&status=settled : {len(ma)} markets  {a.get('_status', a.get('_error',''))}")

        b = get("/markets", {"event_ticker": et, "limit": 100})
        mb = b.get("markets") or []
        print(f"  B. /markets?event_ticker (no status)    : {len(mb)} markets")

        c = get(f"/events/{et}", {"with_nested_markets": "true"})
        ev = c.get("event") or {}
        mc = ev.get("markets") or c.get("markets") or []
        print(f"  C. /events/{{et}}?with_nested_markets    : {len(mc)} markets  {c.get('_status', c.get('_error',''))}")

        # pick whichever returned markets
        markets = ma or mc or mb
        if not markets:
            # D. page series settled markets by close time around the strike date
            mx = int((anchor + pd.Timedelta(days=2)).timestamp())
            mn = int((anchor - pd.Timedelta(days=2)).timestamp())
            d = get("/markets", {"series_ticker": "KXHIGHCHI", "status": "settled",
                                 "min_close_ts": mn, "max_close_ts": mx, "limit": 200})
            md = d.get("markets") or []
            md = [m for m in md if et.split("-")[-1] in (m.get("ticker") or "")] or md
            print(f"  D. /markets?series&status=settled&close_ts window : {len(md)} markets  {d.get('_status', d.get('_error',''))}")
            markets = md

        if not markets:
            print("  -> NO retrieval method returned markets for this event.\n")
            continue

        print(f"  -> using {len(markets)} markets; per-bucket trade tape + candlesticks:")
        print(f"     {'ticker':<26}{'trades':>8}{'pre1pm':>8}{'candles':>9}  {'settled':>8}")
        formable = 0
        for m in markets:
            tkr = m.get("ticker")
            if not tkr:
                continue
            n, pre = count_trades(tkr, anchor)
            cn = candle_count("KXHIGHCHI", tkr, ds)
            if pre > 0:
                formable += 1
            print(f"     {tkr:<26}{n:>8}{pre:>8}{cn:>9}  {str(m.get('result') or m.get('settlement_value')):>8}")
        print(f"  -> {formable} buckets have >=1 pre-1PM trade  "
              f"({'WING FORMABLE' if formable >= 2 else 'not formable'})\n")

    print("If A/C/D return the 6 settled buckets and trades>0 pre-1PM => deep history IS pullable;")
    print("backfill just needs status=settled (or candlesticks) instead of the default /markets call.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
