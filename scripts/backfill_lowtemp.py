"""Keyless, windowed, multi-city Kalshi LOW-temp trade-history backfill.

Mirror of `scripts/backfill_cities.py` (the authed daily-HIGH backfill) but for the daily-LOW
series (KXLOWT*), and KEYLESS — the public market-data endpoints (/events, /markets,
/markets/trades) need no auth, so this runs on a laptop with no Kalshi key. It is slower than the
authed tier (lower rate limit) so every GET has 429/5xx exponential backoff + polite spacing.

Same storage shape + parquet schema as the high backfill, so `late_night_coverage.py` reads HIGH
and LOW identically:
  <out>/<SERIES>/<YYYY-MM-DD>.zip  (one parquet per event-day; raw deleted; resume-safe).

The LOW-temp markets are NEW (KXLOWT* only): ~183 settled days for the first batch
(CHI/MIA/AUS/DEN/PHIL/LAX), ~72 for the rest; NYC has no low-temp series. Truth is Kalshi's own
settlement_value (the bucket that settled 'yes'); event_date is parsed from the ticker (lesson #7).

Usage:
  python scripts/backfill_lowtemp.py                 # all 19 low series, ~400d window
  python scripts/backfill_lowtemp.py --days 400 --series KXLOWTCHI KXLOWTMIA
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests

_ROOT = Path(__file__).resolve().parent.parent
API = "https://api.elections.kalshi.com/trade-api/v2"
# store alongside the relocated HIGH backfill so one root holds both (KXLOWT* vs KXHIGH* never collide)
OUT_DIR = _ROOT.parent / "data" / "weather" / "backfill"
UA = {"User-Agent": "weather-alpha-lowtemp-backfill/1.0", "Accept": "application/json"}

# 19 resolved daily-LOW series -> city label (KXLOWTNY does not exist; NYC has no low-temp market).
CITIES = {
    "KXLOWTCHI": "Chicago", "KXLOWTMIA": "Miami", "KXLOWTAUS": "Austin", "KXLOWTDEN": "Denver",
    "KXLOWTPHIL": "Philadelphia", "KXLOWTLAX": "Los Angeles", "KXLOWTLV": "Las Vegas",
    "KXLOWTNOLA": "New Orleans", "KXLOWTSEA": "Seattle", "KXLOWTSFO": "San Francisco",
    "KXLOWTDC": "Washington DC", "KXLOWTATL": "Atlanta", "KXLOWTMIN": "Minneapolis",
    "KXLOWTPHX": "Phoenix", "KXLOWTBOS": "Boston", "KXLOWTDAL": "Dallas",
    "KXLOWTOKC": "Oklahoma City", "KXLOWTSATX": "San Antonio", "KXLOWTHOU": "Houston",
}

_SESSION = requests.Session()
_SESSION.headers.update(UA)


def get(path: str, params: dict | None = None, max_attempts: int = 8) -> dict:
    """Keyless GET API+path with 429/5xx exponential backoff + polite spacing."""
    r = None
    for attempt in range(max_attempts):
        r = _SESSION.get(API + path, params=params, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.5 * (2 ** attempt))
            continue
        r.raise_for_status()
        time.sleep(0.06)  # polite spacing between successful calls
        return r.json()
    raise RuntimeError(f"{path} failed after {max_attempts} attempts (last {r.status_code if r else '?'})")


def event_date_from_ticker(event_ticker: str):
    """KXLOWTCHI-26JUN13 -> Timestamp(2026-06-13). Ticker pattern, not API settlement_date (lesson #7)."""
    try:
        return pd.Timestamp(datetime.strptime(event_ticker.split("-", 1)[1], "%y%b%d").date())
    except Exception:
        return None


def iter_events(series: str, cutoff: pd.Timestamp):
    cursor = None
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        d = get("/events", p)
        evs = d.get("events", [])
        for e in evs:
            et = e.get("event_ticker", "")
            ed = event_date_from_ticker(et)
            if ed is not None and ed >= cutoff:
                yield et, ed
        cursor = d.get("cursor") or None
        if not cursor or not evs:
            return


def iter_trades(ticker: str, max_pages: int = 120):
    cursor, pages = None, 0
    while True:
        p = {"ticker": ticker, "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        d = get("/markets/trades", p)
        trades = d.get("trades", [])
        for t in trades:
            yield t
        cursor = d.get("cursor") or None
        pages += 1
        if not cursor or not trades or pages >= max_pages:
            return


def _rows_for_event(event_ticker: str, ed: pd.Timestamp) -> list[dict]:
    rows: list[dict] = []
    markets = get("/markets", {"event_ticker": event_ticker, "limit": 100}).get("markets", [])
    for m in markets:
        tkr = m.get("ticker")
        if not tkr:
            continue
        for t in iter_trades(tkr):
            yc, nc = t.get("yes_price_dollars"), t.get("no_price_dollars")
            try:
                yes_cents = int(round(float(yc) * 100)) if yc is not None else t.get("yes_price")
                no_cents = int(round(float(nc) * 100)) if nc is not None else t.get("no_price")
            except (TypeError, ValueError):
                yes_cents = no_cents = None
            rows.append({
                "event_ticker": event_ticker, "event_date": ed.normalize(), "ticker": tkr,
                "subtitle": m.get("subtitle") or m.get("yes_sub_title") or "",
                "settlement_value": m.get("settlement_value") or m.get("result"),
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


def backfill_series(series: str, cutoff: pd.Timestamp) -> tuple[int, int]:
    city = CITIES[series]
    city_dir = OUT_DIR / series
    n_done = n_new = n_fail = 0
    for et, ed in iter_events(series, cutoff):
        ds = ed.strftime("%Y-%m-%d")
        if (city_dir / f"{ds}.zip").exists():
            n_done += 1
            continue
        try:
            rows = _rows_for_event(et, ed)
        except RuntimeError as e:
            print(f"  [{series}] {ds} FAILED ({e}); will retry next run", flush=True)
            n_fail += 1
            continue
        if not rows:
            continue
        df = pd.DataFrame(rows).drop_duplicates(
            subset=["ticker", "timestamp", "yes_price_cents", "count"], keep="last")
        df = df.sort_values(["ticker", "timestamp"]).reset_index(drop=True)
        _write_day_zip(city_dir, ds, df)
        n_new += 1
        if n_new % 25 == 0:
            print(f"  [{series}] {city}: {n_new} new day-zips ({ds} latest)", flush=True)
    print(f"== {series} ({city}) done: {n_new} new, {n_done} cached, {n_fail} failed ==", flush=True)
    return n_new, n_fail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--series", nargs="*", default=list(CITIES))
    args = ap.parse_args(argv)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = (pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(days=args.days)).normalize()
    print(f"LOW-temp backfill (keyless): window event_date>={cutoff.date()} | "
          f"{len(args.series)} series | out={OUT_DIR}", flush=True)
    t0 = time.time()
    tot_new = tot_fail = 0
    for s in args.series:
        if s not in CITIES:
            print(f"  skip unknown series {s}", flush=True)
            continue
        n_new, n_fail = backfill_series(s, cutoff)
        tot_new += n_new
        tot_fail += n_fail
    print(f"\nPASS DONE in {time.time() - t0:.0f}s: {tot_new} new zips, {tot_fail} failed days.", flush=True)
    (OUT_DIR / "_LOWTEMP_DONE").write_text("complete\n") if tot_fail == 0 else None
    return 0


if __name__ == "__main__":
    sys.exit(main())
