"""DECISIVE probe: does Kalshi's HISTORICAL data tier serve deep KXHIGHCHI price history?

The live /markets + /candlesticks are windowed to ~67d. Kalshi partitions data into live vs
historical; archived data lives under a SEPARATE /historical/* family (no auth):
  GET /historical/cutoff                              live/historical boundary timestamps
  GET /historical/markets?series_ticker=...           settled markets older than cutoff (paged)
  GET /historical/markets/{ticker}/candlesticks       OHLC for an archived market (start/end/period)
  GET /historical/trades?ticker=...                   trade tape for an archived market

If historical/markets reaches 2021 and candlesticks/trades return for an OLD Chicago market,
then deep price history IS pullable -> the backtest can be extended to years.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.probe_kalshi_history_depth import get

CT = ZoneInfo("America/Chicago")


def cutoff():
    for path in ("/historical/cutoff", "/historical/cutoff-timestamps"):
        d = get(path)
        if "_error" not in d and d.get("_status") != 404:
            return path, d
    return None, {}


def page_hist_markets(series, max_pages=40):
    cur, out, pages = None, [], 0
    while True:
        p = {"series_ticker": series, "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = get("/historical/markets", p)
        if d.get("_status") == 404 or "_error" in d:
            return out, pages, d
        mk = d.get("markets") or []
        for m in mk:
            ct = m.get("close_time") or m.get("expiration_time") or m.get("latest_expiration_time")
            out.append((m.get("ticker", ""), pd.to_datetime(ct, utc=True, errors="coerce")))
        cur = d.get("cursor") or None
        pages += 1
        if not cur or not mk or pages >= max_pages:
            return out, pages, d


def hist_candles(ticker, around, period=60):
    if around is pd.NaT or around is None:
        return -1, "no-date"
    p = {"start_ts": int((around - pd.Timedelta(days=1)).timestamp()),
         "end_ts": int((around + pd.Timedelta(days=1)).timestamp()), "period_interval": period}
    d = get(f"/historical/markets/{ticker}/candlesticks", p)
    if d.get("_status") == 404 or "_error" in d:
        return -1, d.get("_status", d.get("_error"))
    return len(d.get("candlesticks") or []), "ok"


def hist_trades(ticker, anchor_utc, max_pages=40):
    cur, n, pre, pages = None, 0, 0, 0
    while True:
        p = {"ticker": ticker, "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = get("/historical/trades", p)
        if d.get("_status") == 404 or "_error" in d:
            return -1, -1
        ts = d.get("trades") or []
        for t in ts:
            n += 1
            tt = pd.to_datetime(t.get("created_time"), utc=True, errors="coerce")
            if tt is not pd.NaT and tt <= anchor_utc:
                pre += 1
        cur = d.get("cursor") or None
        pages += 1
        if not cur or not ts or pages >= max_pages:
            return n, pre


def main():
    print("=" * 88)
    print("KALSHI HISTORICAL-TIER PROBE")
    print("=" * 88)

    path, cut = cutoff()
    print(f"\nGET {path or '/historical/cutoff'} -> {cut}")
    for k, v in (cut or {}).items():
        if k.endswith("_ts") and isinstance(v, (int, float)):
            print(f"    {k}: {v}  = {pd.to_datetime(v, unit='s', utc=True)}")

    print("\nGET /historical/markets?series_ticker=KXHIGHCHI (paginate) ...")
    mk, pages, last = page_hist_markets("KXHIGHCHI")
    mk = [(t, d) for t, d in mk if t]
    if not mk:
        print(f"  -> 0 historical markets. last response: {last}")
        # try legacy series name
        mk2, _, last2 = page_hist_markets("HIGHCHI")
        print(f"  HIGHCHI series -> {len(mk2)} markets. {last2 if not mk2 else ''}")
        mk = [(t, d) for t, d in mk2 if t] or mk
    if not mk:
        print("\n  No historical markets via series filter; cannot confirm depth this way.")
        return 0

    dated = sorted([(t, d) for t, d in mk if d is not pd.NaT], key=lambda x: x[1])
    print(f"  {len(mk)} historical markets over {pages} pages")
    if dated:
        print(f"  oldest close: {dated[0][1]}  ({dated[0][0]})")
        print(f"  newest close: {dated[-1][1]}  ({dated[-1][0]})")

    # Pull candlesticks + trades for the OLDEST and a MID market to prove prices exist.
    picks = []
    if dated:
        picks = [("oldest", dated[0]), ("middle", dated[len(dated) // 2])]
    print("\n  per-market price retrieval (candlesticks + trade tape):")
    print(f"     {'when':<8}{'ticker':<28}{'candles':>9}{'trades':>8}{'pre1pm':>8}")
    for label, (tkr, dt) in picks:
        nc, _ = hist_candles(tkr, dt)
        local = dt.tz_convert(CT)
        anchor = pd.Timestamp(local.year, local.month, local.day, 13, tz=CT).tz_convert("UTC")
        nt, pre = hist_trades(tkr, anchor)
        print(f"     {label:<8}{tkr:<28}{nc:>9}{nt:>8}{pre:>8}")

    print("\nVERDICT: if oldest close reaches 2021-2023 and candles/trades > 0 => DEEP HISTORY IS")
    print("PULLABLE via /historical/*. The backtest is NOT capped at 67 days; backfill must be")
    print("rewritten to read the historical tier (markets list -> per-ticker candlesticks/trades).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
