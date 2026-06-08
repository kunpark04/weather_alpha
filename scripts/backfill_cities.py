"""Authenticated, windowed, multi-city Kalshi trade-history backfill -> data/backfill/.

Pulls ~1 year of settled events + their trades for every city daily-high temp series, using
RSA-PSS request signing (read-only key) for the higher authenticated rate-limit tier.

STORAGE + RESUME (designed to run unattended on the small droplet):
  * One zip PER CITY PER DAY: data/backfill/<SERIES>/<YYYY-MM-DD>.zip (holds that day's parquet).
  * After a day's parquet is written it is zipped and the raw parquet DELETED (save disk).
  * A day whose .zip already exists is SKIPPED -> a crash-restart resumes where it left off
    instead of restarting from zero. Only one event-day is held in memory at a time (flat RAM).
  * Zips are written atomically (.tmp + os.replace) so a crash mid-write never leaves a
    half-written zip that would be mistaken for "done".

Reads KALSHI_KEY_ID + KALSHI_PRIVATE_KEY_PATH from the env (point at the READ-ONLY key).
Signs `timestamp_ms + METHOD + /trade-api/v2<path>` (path only, no query) as kalshi.py does.

Usage:
  python scripts/backfill_cities.py                 # all cities, ~365d window
  python scripts/backfill_cities.py --days 365 --series KXHIGHNY KXHIGHCHI
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import sys
import time
import zipfile
from pathlib import Path

import pandas as pd
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from scripts.kalshi_history import parse_event_date  # reuse ticker-date parser

API = "https://api.elections.kalshi.com/trade-api/v2"
BASE_PATH = "/trade-api/v2"
OUT_DIR = _ROOT / "data" / "backfill"

# 20 resolved city daily-high temp series -> city label (active ticker per city; legacy
# dupes KXHIGHHOU/KXHIGHOU/KXHIGHNY0/KXHIGHTEMPDEN dropped).
CITIES = {
    "KXHIGHNY": "NYC", "KXHIGHCHI": "Chicago", "KXHIGHMIA": "Miami", "KXHIGHAUS": "Austin",
    "KXHIGHDEN": "Denver", "KXHIGHPHIL": "Philadelphia", "KXHIGHLAX": "Los Angeles",
    "KXHIGHTLV": "Las Vegas", "KXHIGHTNOLA": "New Orleans", "KXHIGHTSEA": "Seattle",
    "KXHIGHTSFO": "San Francisco", "KXHIGHTDC": "Washington DC", "KXHIGHTATL": "Atlanta",
    "KXHIGHTMIN": "Minneapolis", "KXHIGHTPHX": "Phoenix", "KXHIGHTBOS": "Boston",
    "KXHIGHTDAL": "Dallas", "KXHIGHTOKC": "Oklahoma City", "KXHIGHTSATX": "San Antonio",
    "KXHIGHTHOU": "Houston",
}

_KID: str = ""
_KEY: rsa.RSAPrivateKey | None = None
_SESSION = requests.Session()


def _load_key() -> None:
    global _KID, _KEY
    _KID = os.environ.get("KALSHI_KEY_ID", "").strip()
    kpath = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "").strip()
    if not _KID or not kpath:
        sys.exit("Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH (read-only key).")
    k = serialization.load_pem_private_key(Path(kpath).read_bytes(), password=None)
    assert isinstance(k, rsa.RSAPrivateKey)
    _KEY = k


def signed_get(path: str, params: dict | None = None, max_attempts: int = 7) -> dict:
    """GET API+path with RSA-PSS auth headers and 429/5xx backoff. Signs path only (no query)."""
    r = None
    for attempt in range(max_attempts):
        ts = int(time.time() * 1000)
        sig = base64.b64encode(_KEY.sign(
            f"{ts}GET{BASE_PATH}{path}".encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
            hashes.SHA256())).decode()
        h = {"KALSHI-ACCESS-KEY": _KID, "KALSHI-ACCESS-SIGNATURE": sig,
             "KALSHI-ACCESS-TIMESTAMP": str(ts), "Accept": "application/json"}
        r = _SESSION.get(API + path, params=params, headers=h, timeout=30)
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(0.4 * (2 ** attempt))
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{path} failed after {max_attempts} attempts (last {r.status_code if r else '?'})")


def iter_events(series: str, cutoff: pd.Timestamp):
    """Yield (event, event_date) for settled events with event_date >= cutoff."""
    cursor = None
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 200}
        if cursor:
            p["cursor"] = cursor
        d = signed_get("/events", p)
        evs = d.get("events", [])
        for e in evs:
            ed = parse_event_date(e)
            if ed is not None and ed >= cutoff:
                yield e, ed
        cursor = d.get("cursor") or None
        if not cursor or not evs:
            return


def iter_trades(ticker: str, max_pages: int = 120):
    cursor, pages = None, 0
    while True:
        p = {"ticker": ticker, "limit": 1000}
        if cursor:
            p["cursor"] = cursor
        d = signed_get("/markets/trades", p)
        trades = d.get("trades", [])
        for t in trades:
            yield t
        cursor = d.get("cursor") or None
        pages += 1
        if not cursor or not trades or pages >= max_pages:
            return


def _rows_for_event(event_ticker: str, ed: pd.Timestamp) -> list[dict]:
    rows: list[dict] = []
    markets = signed_get("/markets", {"event_ticker": event_ticker, "limit": 100}).get("markets", [])
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
                "subtitle": m.get("subtitle") or "",
                "settlement_value": m.get("settlement_value") or m.get("result"),
                "strike_type": m.get("strike_type"), "floor_strike": m.get("floor_strike"),
                "timestamp": pd.to_datetime(t.get("created_time"), utc=True, errors="coerce"),
                "yes_price_cents": yes_cents, "no_price_cents": no_cents,
                "count": t.get("count_fp") or t.get("count"),
                "taker_side": t.get("taker_outcome_side") or t.get("taker_side"),
            })
    return rows


def _write_day_zip(city_dir: Path, ds: str, df: pd.DataFrame) -> int:
    """Write df as parquet INTO <city_dir>/<ds>.zip atomically, no raw parquet left behind."""
    city_dir.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False)              # parquet bytes in memory (no temp parquet on disk)
    tmp = city_dir / f"{ds}.zip.tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{ds}.parquet", buf.getvalue())
    os.replace(tmp, city_dir / f"{ds}.zip")      # atomic publish
    return len(df)


def backfill_series(series: str, cutoff: pd.Timestamp) -> tuple[int, int]:
    """Returns (n_new_zips, n_failed_days)."""
    city = CITIES[series]
    city_dir = OUT_DIR / series
    n_done = n_new = n_fail = 0
    for ev, ed in iter_events(series, cutoff):
        ds = ed.strftime("%Y-%m-%d")
        if (city_dir / f"{ds}.zip").exists():
            n_done += 1
            continue                              # resume: already pulled
        try:
            rows = _rows_for_event(ev.get("event_ticker", ""), ed)
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
        if n_new % 20 == 0:
            print(f"  [{series}] {city}: {n_new} new day-zips ({ds} latest)", flush=True)
    print(f"== {series} ({city}) done: {n_new} new, {n_done} cached, {n_fail} failed ==", flush=True)
    return n_new, n_fail


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--series", nargs="*", default=list(CITIES))
    args = ap.parse_args(argv)
    _load_key()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    done_flag = OUT_DIR / "_DONE"
    done_flag.unlink(missing_ok=True)            # cleared at the start of every pass
    cutoff = (pd.Timestamp.now("UTC").tz_localize(None) - pd.Timedelta(days=args.days)).normalize()
    print(f"Backfill: window event_date>={cutoff.date()} | {len(args.series)} series | out={OUT_DIR}", flush=True)
    t0 = time.time()
    tot_new = tot_fail = 0
    for s in args.series:
        if s not in CITIES:
            print(f"  skip unknown series {s}", flush=True)
            continue
        n_new, n_fail = backfill_series(s, cutoff)
        tot_new += n_new
        tot_fail += n_fail
    msg = f"\nPASS DONE in {time.time() - t0:.0f}s: {tot_new} new zips, {tot_fail} failed days."
    if tot_fail == 0:
        done_flag.write_text("complete\n")        # sentinel: a clean pass with no failures
        msg += "  -> _DONE written (fully complete)."
    else:
        msg += "  -> failures remain; launcher will re-run to retry them."
    print(msg, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
