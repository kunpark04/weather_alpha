"""Probe: how far back does Kalshi temperature-market price history ACTUALLY go?

The repo's backfill only pulled the 20 CURRENT series (KXHIGHCHI, ...). This asks the public
API directly to decide whether ~67 days is a real launch-age cap or a collection artifact
(older data hiding under LEGACY ticker generations, e.g. HIGHCHI / KXTEMPCHIH / KXDENHIGH).

  1. Enumerate every series (no params) and print the temperature-ish tickers.
  2. For every candidate city daily-high/temp series across ALL naming generations, pull ALL
     settled events with NO date cutoff -> count + earliest -> latest event_date (the true depth).
  3. Month histogram for the 3 deepest series (continuity / year-round check).

No auth (public market data, same endpoint as kalshi_history.py). Read-only; writes nothing.
Crash-proof: every series probe is wrapped so one bad response can't abort the run.
"""
from __future__ import annotations

import sys
import time
import traceback
from collections import Counter
from pathlib import Path

import pandas as pd
import requests

API = "https://api.elections.kalshi.com/trade-api/v2"
UA = "weather-alpha-probe/0.1"

# Curated city daily-high / temp series across ALL naming generations seen in /series enumeration.
# Grouped by city so the depth table reads clearly (current KX* + legacy no-KX + KXTEMP*H + *HIGH).
CITY_SERIES = [
    # Chicago
    "KXHIGHCHI", "HIGHCHI", "KXTEMPCHIH",
    # New York
    "KXHIGHNY", "HIGHNY", "HIGHNY0", "KXHIGHNY0", "KXHIGHNYD", "KXTEMPNYCH",
    # Denver
    "KXHIGHDEN", "KXDENHIGH", "KXHIGHTEMPDEN", "KXDVHIGH",
    # Houston
    "KXHIGHTHOU", "KXHIGHHOU", "KXHIGHOU", "KXHOUHIGH",
    # Miami
    "KXHIGHMIA", "HIGHMIA", "KXTEMPMIAH",
    # Austin
    "KXHIGHAUS", "HIGHAUS",
    # Philadelphia
    "KXHIGHPHIL", "KXPHILHIGH",
    # Los Angeles
    "KXHIGHLAX", "KXTEMPLAXH",
    # Washington DC
    "KXHIGHTDC", "KXTEMPDCH",
    # Boston
    "KXHIGHTBOS", "KXTEMPBOSH",
    # remaining current cities
    "KXHIGHTLV", "KXHIGHTNOLA", "KXHIGHTSEA", "KXHIGHTSFO", "KXHIGHTATL", "KXHIGHTMIN",
    "KXHIGHTPHX", "KXHIGHTDAL", "KXHIGHTOKC", "KXHIGHTSATX",
    # national / misc temp
    "KXHIGHUS", "HIGHUS", "KXMAXTEMP100", "KXMICHTEMP", "MICHTEMP", "KXTEMP", "TEMP", "KXAVGTEMP",
]

CITY_OF = {
    "CHI": "Chicago", "NY": "New York", "DEN": "Denver", "DV": "Denver?", "HOU": "Houston",
    "OU": "Houston", "MIA": "Miami", "AUS": "Austin", "PHIL": "Philadelphia", "LAX": "LosAngeles",
    "DC": "WashDC", "BOS": "Boston", "TLV": "LasVegas", "TNOLA": "NewOrleans", "TSEA": "Seattle",
    "TSFO": "SanFran", "TATL": "Atlanta", "TMIN": "Minneapolis", "TPHX": "Phoenix", "TDAL": "Dallas",
    "TOKC": "OKC", "TSATX": "SanAntonio", "US": "National",
}


def get(path, params=None, attempts=4):
    url = f"{API}{path}"
    last = None
    for a in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=30,
                             headers={"User-Agent": UA, "Accept": "application/json"})
            if r.status_code == 404:
                return {"_status": 404}
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(0.5 * 2 ** a); continue
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            last = e; time.sleep(0.5 * 2 ** a)
    return {"_error": str(last)}


def ev_date(ev):
    t = ev.get("event_ticker", "")
    p = t.split("-")
    if len(p) >= 2:
        ts = pd.to_datetime(p[1], format="%y%b%d", errors="coerce")
        if ts is not pd.NaT:
            return ts.normalize()
    return None


def all_settled_dates(series):
    """Every settled-event date for series, no cutoff. None if series 404/errors; [] if empty."""
    cur, dates, pages = None, [], 0
    while True:
        p = {"series_ticker": series, "status": "settled", "limit": 200}
        if cur:
            p["cursor"] = cur
        d = get("/events", p)
        if d.get("_status") == 404 or "_error" in d:
            return None if not dates else dates
        for e in d.get("events") or []:
            dt = ev_date(e)
            if dt is not None:
                dates.append(dt)
        cur = d.get("cursor") or None
        pages += 1
        if not cur or pages > 80:
            break
    return sorted(dates)


def list_series():
    """Enumerate all series (no params); print + return the temperature-ish tickers."""
    print("\n--- /series enumeration (no params) ---")
    d = get("/series")
    ser = d.get("series") or []
    hot = sorted({s.get("ticker", "") for s in ser
                  if "HIGH" in s.get("ticker", "").upper() or "TEMP" in s.get("ticker", "").upper()})
    print(f"  {len(ser)} series total, {len(hot)} temp-ish")
    print("  " + ", ".join(hot))
    return set(hot)


def main():
    print("=" * 92)
    print("KALSHI TEMPERATURE-MARKET HISTORY DEPTH PROBE  (public API, no auth)")
    print("=" * 92)

    try:
        discovered = list_series()
    except Exception:
        discovered = set()
        traceback.print_exc()

    # Probe depth for the curated city list PLUS any discovered temp-ish ticker not already in it.
    extra = sorted(discovered - set(CITY_SERIES))
    probe = CITY_SERIES + extra

    results = {}
    print("\n--- settled-event depth per series (count | earliest -> latest) ---")
    for s in probe:
        try:
            dates = all_settled_dates(s)
        except Exception as e:
            print(f"  {s:<16} EXC {e}")
            continue
        if dates:
            results[s] = dates

    # Print curated city series first (sorted by earliest date so deepest history floats up).
    def fmt(s):
        d = results[s]
        return (f"{s:<16}{len(d):>7}   {d[0].date()!s:<12} -> {d[-1].date()!s:<12}"
                f"  span {(d[-1] - d[0]).days + 1}d")

    print("\nCITY daily-high / temp series (sorted by EARLIEST settled event):")
    print(f"  {'series':<16}{'nEv':>7}   {'earliest':<12}    {'latest':<12}")
    for s in sorted([s for s in CITY_SERIES if s in results], key=lambda s: results[s][0]):
        print("  " + fmt(s))

    if extra:
        print("\nOther temp-ish series discovered (depth):")
        for s in sorted([s for s in extra if s in results], key=lambda s: results[s][0]):
            print("  " + fmt(s))

    # Month histograms for the 3 deepest series overall.
    deepest = sorted(results, key=lambda s: results[s][0])[:3]
    for s in deepest:
        dates = results[s]
        months = Counter(d.strftime("%Y-%m") for d in dates)
        print(f"\n--- {s}: {len(dates)} settled events, {dates[0].date()} -> {dates[-1].date()} ---")
        for m in sorted(months):
            print(f"    {m}: {months[m]:>3}  {'#' * min(months[m], 60)}")

    print("\nINTERPRETATION:")
    print("  * Any city series with an earliest date << 2026-03-21 => deeper price history EXISTS;")
    print("    67 days is a COLLECTION artifact (we only backfilled the current KX* tickers).")
    print("    Pulling that legacy ticker's trades extends the backtest by however many days.")
    print("  * Continuous month histogram (no gaps) => the market runs year-round, not seasonally.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
