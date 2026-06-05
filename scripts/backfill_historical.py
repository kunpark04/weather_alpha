"""Public HISTORICAL-tier backfill: pull FULL price history (back to 2021) for all city series
into data/backfill/<SERIES>/<date>.zip -- the SAME schema + dir as backfill_cities, so every
existing backtest (load_city, rca_city, the wing backtests) reads the deep history UNCHANGED.

WHY: the live /markets + /markets/trades endpoints are windowed to ~67 days (Kalshi's "live"
tier). Kalshi keeps every market/trade since 2021 under a separate "/historical/*" family
(no auth). This reads that tier:
  GET /historical/cutoff                       -> live/historical boundary (market_settled_ts)
  GET /historical/markets?series_ticker=S      -> all archived settled markets for S (paged)
  GET /historical/markets/{t}/candlesticks     -> (not used here; trades chosen for schema parity)
  GET /historical/trades?ticker=T              -> archived trade tape (paged)
For days >= cutoff (the recent ~67d) the data is already on disk from the live backfill, so
those date-zips are simply skipped (resume).

RESUME / SAFETY: one zip per city per event-day; a day whose <date>.zip already exists is
SKIPPED, so this coexists with the live data already on disk and is crash-restartable. Zips are
written atomically. Public endpoints, no auth, exponential backoff on 429/5xx. Connection reuse
via a Session (tens of thousands of calls across the deep cities).

Usage:
  python scripts/backfill_historical.py                        # all 20 cities, full depth
  python scripts/backfill_historical.py --series KXHIGHCHI     # one city
  python scripts/backfill_historical.py --series KXHIGHCHI --max-events 2   # smoke test
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import pandas as pd
import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from scripts.backfill_cities import CITIES                       # 20 series -> city label
from scripts.kalshi_history import parse_event_date              # ticker-date parser

API = "https://api.elections.kalshi.com/trade-api/v2"
OUT_DIR = _ROOT / "data" / "backfill"
UA = "weather-alpha-historical/0.1"
_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": UA, "Accept": "application/json"})


def _get(path: str, params: dict | None = None, attempts: int = 6) -> dict:
    last = None
    for a in range(attempts):
        try:
            r = _SESSION.get(API + path, params=params, timeout=30)
            if r.status_code == 404:
                return {"_status": 404}
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(0.4 * 2 ** a); continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e; time.sleep(0.4 * 2 ** a)
    return {"_error": str(last)}


def cutoff_ts() -> pd.Timestamp:
    d = _get("/historical/cutoff")
    v = d.get("market_settled_ts")
    ts = pd.to_datetime(v, utc=True, errors="coerce") if v else pd.NaT
    return ts if ts is not pd.NaT else pd.Timestamp("2026-04-05", tz="UTC")


def all_historical_markets(series: str, max_pages: int = 60) -> list[dict]:
    """Every archived settled market for the series (paged). Each dict carries ticker/subtitle/
    settlement(result)/strike fields + close_time, same as the live /markets objects."""
    cur, out, pages = None, [], 0
    while True:
        p = {"series_ticker": series, "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = _get("/historical/markets", p)
        if d.get("_status") == 404 or "_error" in d:
            break
        mk = d.get("markets") or []
        out.extend(mk)
        cur = d.get("cursor") or None
        pages += 1
        if not cur or not mk or pages >= max_pages:
            break
    return out


def live_settled_markets(series: str, max_pages: int = 30) -> list[dict]:
    """The recent (>= cutoff) settled markets from the live tier, to close the gap to today."""
    cur, out, pages = None, [], 0
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = _get("/markets", p)
        if d.get("_status") == 404 or "_error" in d:
            break
        mk = d.get("markets") or []
        out.extend(mk)
        cur = d.get("cursor") or None
        pages += 1
        if not cur or not mk or pages >= max_pages:
            break
    return out


def iter_trades(ticker: str, historical: bool, max_pages: int = 30):
    path = "/historical/trades" if historical else "/markets/trades"
    cur, pages = None, 0
    while True:
        p = {"ticker": ticker, "limit": 1000}
        if cur:
            p["cursor"] = cur
        d = _get(path, p)
        if d.get("_status") == 404 or "_error" in d:
            return
        ts = d.get("trades") or []
        for t in ts:
            yield t
        cur = d.get("cursor") or None
        pages += 1
        if not cur or not ts or pages >= max_pages:
            return


def _rows_for_market(m: dict, event_ticker: str, ed: pd.Timestamp, cutoff: pd.Timestamp) -> list[dict]:
    tkr = m.get("ticker")
    if not tkr:
        return []
    close = pd.to_datetime(m.get("close_time") or m.get("expiration_time"), utc=True, errors="coerce")
    historical = (close is pd.NaT) or (close < cutoff)
    sub = m.get("subtitle") or m.get("yes_sub_title") or ""
    settle = m.get("settlement_value") or m.get("result")
    rows = []
    for t in iter_trades(tkr, historical):
        yc, nc = t.get("yes_price_dollars"), t.get("no_price_dollars")
        try:
            yes_cents = int(round(float(yc) * 100)) if yc is not None else t.get("yes_price")
            no_cents = int(round(float(nc) * 100)) if nc is not None else t.get("no_price")
        except (TypeError, ValueError):
            yes_cents = no_cents = None
        rows.append({
            "event_ticker": event_ticker, "event_date": ed.normalize(), "ticker": tkr,
            "subtitle": sub, "settlement_value": settle,
            "strike_type": m.get("strike_type"), "floor_strike": m.get("floor_strike"),
            "timestamp": pd.to_datetime(t.get("created_time"), utc=True, errors="coerce"),
            "yes_price_cents": yes_cents, "no_price_cents": no_cents,
            "count": t.get("count_fp") or t.get("count"),
            "taker_side": t.get("taker_outcome_side") or t.get("taker_side"),
        })
    return rows


def _write_day_zip(city_dir: Path, ds: str, df: pd.DataFrame) -> int:
    city_dir.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)
    tmp = city_dir / f"{ds}.zip.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{ds}.parquet", buf.getvalue())
    os.replace(tmp, city_dir / f"{ds}.zip")
    return len(df)


def backfill_series(series: str, cutoff: pd.Timestamp, since: pd.Timestamp,
                    max_events: int | None) -> tuple[int, int, int]:
    city = CITIES[series]
    city_dir = OUT_DIR / series

    markets = all_historical_markets(series)
    markets += live_settled_markets(series)
    by_ticker = {m.get("ticker"): m for m in markets if m.get("ticker")}  # dedup by ticker

    events: dict[str, list[dict]] = defaultdict(list)
    for m in by_ticker.values():
        et = m.get("event_ticker") or m["ticker"].rsplit("-", 1)[0]
        events[et].append(m)

    # Keep only events >= `since` (the modern 6-bucket era; pre-2023 are 1-4-contract threshold
    # ladders the wing parser can't read). Sort ascending so a partial run resumes oldest-first.
    dated = sorted(((et, ed) for et in events
                    if (ed := parse_event_date({"event_ticker": et})) is not None and ed >= since),
                   key=lambda x: x[1])
    if max_events is not None:
        dated = dated[:max_events]

    print(f"== {series} ({city}): {len(by_ticker)} markets, {len(dated)} events >= {since.date()} ==",
          flush=True)
    n_new = n_skip = n_fail = 0
    for et, ed in dated:
        ds = ed.strftime("%Y-%m-%d")
        if (city_dir / f"{ds}.zip").exists():
            n_skip += 1
            continue
        try:
            rows: list[dict] = []
            for m in events[et]:
                rows.extend(_rows_for_market(m, et, ed, cutoff))
        except Exception as e:                       # noqa: BLE001 - never let one day abort the city
            print(f"  [{series}] {ds} FAILED ({e}); retry next run", flush=True)
            n_fail += 1
            continue
        if not rows:
            continue
        df = pd.DataFrame(rows).drop_duplicates(
            subset=["ticker", "timestamp", "yes_price_cents", "count"], keep="last")
        df = df.sort_values(["ticker", "timestamp"]).reset_index(drop=True)
        _write_day_zip(city_dir, ds, df)
        n_new += 1
        if n_new % 50 == 0:
            print(f"  [{series}] {city}: {n_new} new day-zips ({ds} latest)", flush=True)
    print(f"== {series} done: {n_new} new, {n_skip} cached, {n_fail} failed ==", flush=True)
    return n_new, n_skip, n_fail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", nargs="*", default=list(CITIES))
    ap.add_argument("--since", default="2023-01-01",
                    help="skip events before this date (default = modern 6-bucket era start)")
    ap.add_argument("--max-events", type=int, default=None, help="cap events/series (smoke test)")
    args = ap.parse_args(argv)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = cutoff_ts()
    since = pd.Timestamp(args.since).normalize()
    print(f"HISTORICAL backfill -> {OUT_DIR}", flush=True)
    print(f"live/historical cutoff (market_settled_ts) = {cutoff} | since = {since.date()}", flush=True)
    t0 = time.time()
    tot_new = tot_skip = tot_fail = 0
    for s in args.series:
        if s not in CITIES:
            print(f"  skip unknown series {s}", flush=True)
            continue
        try:
            n, sk, f = backfill_series(s, cutoff, since, args.max_events)
        except Exception as e:                       # noqa: BLE001
            print(f"== {s} CRASHED: {e} ==", flush=True)
            continue
        tot_new += n; tot_skip += sk; tot_fail += f
    print(f"\nDONE in {time.time() - t0:.0f}s: {tot_new} new zips, {tot_skip} cached, "
          f"{tot_fail} failed.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
