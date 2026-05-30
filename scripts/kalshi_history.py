"""Pull historical KXHIGHCHI events + trades from Kalshi ->data/kalshi_history.parquet.

One-shot batch job. No auth required for public market data. Idempotent — re-runs append
new events and skip already-pulled tickers (controlled by --refresh).

Usage:
    python scripts/kalshi_history.py                       # full pull (settled events)
    python scripts/kalshi_history.py --series KXHIGHCHI    # explicit series
    python scripts/kalshi_history.py --max-events 50       # smoke test
    python scripts/kalshi_history.py --refresh             # force re-pull existing tickers

Output schema (one row per trade):
    event_ticker, event_date, ticker, subtitle, settlement_value,
    timestamp, yes_price_cents, no_price_cents, count, taker_side
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd
import requests

KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
USER_AGENT = "weather-alpha-backtest/0.1"

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = _PROJECT_ROOT / "data" / "kalshi_history.parquet"


def _http_get(path: str, params: dict | None = None, max_attempts: int = 4) -> dict:
    """GET with simple exponential backoff for transient 5xx / rate limits."""
    url = f"{KALSHI_API}{path}"
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            r = requests.get(url, params=params, timeout=30,
                             headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(0.5 * (2 ** attempt))
                continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last_exc = e
            time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError(f"{url} failed after {max_attempts} attempts: {last_exc}")


def iter_events(series_ticker: str, status: str = "settled",
                page_size: int = 200, max_events: int | None = None):
    """Yield events for `series_ticker` with `status`, paginated by cursor."""
    cursor: str | None = None
    seen = 0
    while True:
        params = {"series_ticker": series_ticker, "status": status, "limit": page_size}
        if cursor:
            params["cursor"] = cursor
        data = _http_get("/events", params=params)
        events = data.get("events", [])
        if not events:
            break
        for ev in events:
            yield ev
            seen += 1
            if max_events is not None and seen >= max_events:
                return
        cursor = data.get("cursor") or None
        if not cursor:
            break


def fetch_markets(event_ticker: str) -> list[dict]:
    data = _http_get("/markets", params={"event_ticker": event_ticker, "limit": 100})
    return data.get("markets", [])


def iter_trades(ticker: str, max_pages: int = 50, page_size: int = 1000):
    """Yield trades for a market. Paginated via cursor. Caps at `max_pages` for safety."""
    cursor: str | None = None
    pages = 0
    while True:
        params = {"ticker": ticker, "limit": page_size}
        if cursor:
            params["cursor"] = cursor
        try:
            data = _http_get("/markets/trades", params=params)
        except RuntimeError as e:
            print(f"    !! trades fetch failed for {ticker}: {e}")
            return
        trades = data.get("trades", [])
        if not trades:
            return
        for t in trades:
            yield t
        cursor = data.get("cursor") or None
        pages += 1
        if not cursor or pages >= max_pages:
            return


def parse_event_date(event: dict) -> pd.Timestamp | None:
    """The KXHIGHCHI **strike date** — the day the predicted high happens.

    PREFER ticker pattern (KXHIGHCHI-YYMMMDD) over API fields, because the API
    fields (strike_date, settlement_date, close_time) are next-day settlement
    timestamps — off by 1 from the prediction date. The ticker carries the
    actual strike date that matches our model's OOF prediction date.
    """
    ticker = event.get("event_ticker", "")
    parts = ticker.split("-")
    if len(parts) >= 2:
        ts = pd.to_datetime(parts[1], format="%y%b%d", utc=False, errors="coerce")
        if ts is not pd.NaT:
            return ts.normalize()
    # Last-ditch fallback to API fields (shouldn't normally trigger).
    for key in ("strike_date", "settlement_date", "expiration_time", "close_time"):
        v = event.get(key)
        if v:
            try:
                return pd.to_datetime(v, utc=True, errors="coerce").tz_convert(None).normalize()
            except Exception:
                continue
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="KXHIGHCHI", help="series_ticker to pull")
    ap.add_argument("--status", default="settled", choices=["settled", "open", "active"])
    ap.add_argument("--max-events", type=int, default=None, help="cap on events pulled")
    ap.add_argument("--refresh", action="store_true", help="re-pull already-cached tickers")
    ap.add_argument("--out", default=str(OUT_PATH), help="output parquet path")
    args = ap.parse_args(argv)

    out_path = Path(args.out)
    existing_tickers: set[str] = set()
    if out_path.exists() and not args.refresh:
        existing = pd.read_parquet(out_path)
        existing_tickers = set(existing["ticker"].unique())
        print(f"Resuming: {len(existing_tickers)} tickers already in {out_path.name}")
    else:
        existing = pd.DataFrame()

    rows: list[dict] = []
    n_events = 0
    n_markets = 0
    n_skipped = 0

    print(f"Pulling Kalshi events: series={args.series} status={args.status}")
    for event in iter_events(args.series, status=args.status, max_events=args.max_events):
        n_events += 1
        event_ticker = event.get("event_ticker", "")
        event_date = parse_event_date(event)

        try:
            markets = fetch_markets(event_ticker)
        except RuntimeError as e:
            print(f"  [{n_events}] {event_ticker}  markets fetch FAILED: {e}")
            continue

        for m in markets:
            ticker = m.get("ticker")
            if not ticker:
                continue
            n_markets += 1
            if ticker in existing_tickers:
                n_skipped += 1
                continue

            settlement = m.get("settlement_value") or m.get("result")
            subtitle = m.get("subtitle") or ""

            n_trades_for_ticker = 0
            for t in iter_trades(ticker):
                # Kalshi returns prices as strings in dollars; count as count_fp string.
                try:
                    yes_cents = int(round(float(t.get("yes_price_dollars", 0)) * 100))
                    no_cents = int(round(float(t.get("no_price_dollars", 0)) * 100))
                except (TypeError, ValueError):
                    yes_cents = None
                    no_cents = None
                try:
                    count = float(t.get("count_fp", 0))
                except (TypeError, ValueError):
                    count = None

                rows.append({
                    "event_ticker":     event_ticker,
                    "event_date":       event_date.normalize() if event_date is not None else None,
                    "ticker":           ticker,
                    "subtitle":         subtitle,
                    "settlement_value": settlement,
                    "strike_type":      m.get("strike_type"),
                    "floor_strike":     m.get("floor_strike"),
                    "timestamp":        pd.to_datetime(t.get("created_time"), utc=True, errors="coerce"),
                    "yes_price_cents":  yes_cents,
                    "no_price_cents":   no_cents,
                    "count":            count,
                    "taker_side":       t.get("taker_outcome_side") or t.get("taker_side"),
                })
                n_trades_for_ticker += 1

        if n_events % 25 == 0:
            print(f"  [{n_events}] events processed, {n_markets} markets, "
                  f"{n_skipped} skipped (cached), {len(rows)} new trade rows")

    print(f"\nTotal: {n_events} events, {n_markets} markets, {len(rows)} new trade rows")

    if not rows and existing.empty:
        print("Nothing to write.")
        return 0

    new_df = pd.DataFrame(rows)
    if not existing.empty:
        df = pd.concat([existing, new_df], ignore_index=True, sort=False)
    else:
        df = new_df
    df = df.drop_duplicates(subset=["ticker", "timestamp", "yes_price_cents", "count"], keep="last")
    df = df.sort_values(["event_date", "ticker", "timestamp"]).reset_index(drop=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path)
    print(f"Wrote {len(df)} total rows to {out_path}")
    print(f"  distinct events: {df['event_ticker'].nunique()}")
    print(f"  distinct tickers: {df['ticker'].nunique()}")
    if "event_date" in df.columns and df["event_date"].notna().any():
        print(f"  event_date range: {df['event_date'].min()} ->{df['event_date'].max()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
