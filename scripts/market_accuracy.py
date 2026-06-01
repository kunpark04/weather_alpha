"""Market accuracy by intraday anchor hour, reconstructed from Kalshi candlesticks.

For a temperature series (HIGH or LOW, any city) this measures HOW ACCURATE THE
MARKET is at each local clock hour — the only thing market_wing depends on, since
it is model-free. For each settled day and each local hour h:

    market_modal(h) = argmax_bucket  yes_ask.close  at h:00 local
    Top-1(h)        = modal bucket == the bucket that settled YES
    Top-3(h)        = settled bucket within modal +/- 1 position
    wing sum_asks   = modal_ask + higher-ask adjacent (the drop_lower_ask 2-leg wing)

Truth is the settled bucket result (no CLI needed). Everything uses Kalshi PUBLIC
endpoints (no auth, no model, no weather feed):

    GET /markets?event_ticker=...                      -> 6 buckets + settled result
    GET /series/{s}/markets/{ticker}/candlesticks?...  -> per-bucket yes_ask history

IMPORTANT robustness notes (learned the hard way):
  * Kalshi rate-limits hard (HTTP 429). A failed fetch must NEVER be read as
    "no quote" — that silently drops the favorite and corrupts the modal. We retry
    (honoring Retry-After) and DROP the whole day if any fetch ultimately fails.
  * Candle price fields are named *_dollars (e.g. yes_ask.close_dollars), strings.
  * Kalshi purges markets below a rolling ~60-70 day window, so only the last
    ~2 months are reconstructable today.

Usage:
    python scripts/market_accuracy.py --series KXHIGHCHI --tz America/Chicago --kind high
    python scripts/market_accuracy.py --series KXLOWTNYC --tz America/New_York --kind low
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from weather_alpha.kalshi import _fp  # noqa: E402  (fixed-point string -> float)

API = "https://api.elections.kalshi.com/trade-api/v2"


def _order_key(m: dict) -> float:
    st = m.get("strike_type")
    if st == "less":
        return float("-inf")
    if st == "greater":
        return float("inf")
    fs = m.get("floor_strike")
    return float(fs) if fs is not None else 0.0


def _local_ts(date: pd.Timestamp, hour: int, tz: str) -> int:
    return int(pd.Timestamp(year=date.year, month=date.month, day=date.day, hour=hour, tz=tz).timestamp())


async def _get(client: httpx.AsyncClient, sem: asyncio.Semaphore, path: str, params: dict) -> tuple[bool, dict]:
    """GET with 429/5xx retry (honoring Retry-After). Returns (ok, json).
    ok=False means the fetch ultimately FAILED (caller must drop the day, not treat as empty).
    A 404 is a genuine 'absent' (ok=True, {})."""
    async with sem:
        delay = 0.5
        for _ in range(8):
            try:
                r = await client.get(path, params=params)
            except httpx.TransportError:
                await asyncio.sleep(delay); delay = min(delay * 2, 8.0); continue
            if r.status_code == 200:
                return True, r.json()
            if r.status_code == 404:
                return True, {}
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("Retry-After")
                await asyncio.sleep(float(ra) if ra else delay); delay = min(delay * 2, 8.0); continue
            return False, {}                              # unexpected 4xx
        return False, {}                                  # retries exhausted


async def _day_row(client, sem, series, date, tz, hours):
    """Returns (status, rows): status in {'ok','skip','fail'}. 'skip' = legit no-data
    (purged/unsettled); 'fail' = a fetch failed (rate-limited) so the day is untrusted."""
    ev = f"{series}-{date.strftime('%y%b%d').upper()}"
    ok, mk = await _get(client, sem, "/markets", {"event_ticker": ev, "limit": 100})
    if not ok:
        return "fail", []
    markets = mk.get("markets") or []
    if len(markets) < 5:
        return "skip", []
    markets = sorted(markets, key=_order_key)
    winners = [i for i, m in enumerate(markets) if (m.get("result") or "").lower() == "yes"]
    if len(winners) != 1:
        return "skip", []                                 # not settled / ambiguous
    win_idx = winners[0]

    start_ts = _local_ts(date, min(hours) - 1, tz)
    end_ts = _local_ts(date, max(hours), tz)

    async def bucket_series(m):
        ok_, cs = await _get(client, sem, f"/series/{series}/markets/{m['ticker']}/candlesticks",
                             {"start_ts": start_ts, "end_ts": end_ts, "period_interval": 60,
                              "include_latest_before_start": "true"})
        if not ok_:
            return None                                   # failure -> day will be dropped
        out = {}
        for c in (cs.get("candlesticks") or cs.get("candles") or []):
            ts = c.get("end_period_ts")
            ya = (c.get("yes_ask") or {}).get("close_dollars")
            if ts is not None and ya is not None:
                out[int(ts)] = _fp(ya)
        return out

    series_by_bucket = await asyncio.gather(*[bucket_series(m) for m in markets])
    if any(sb is None for sb in series_by_bucket):
        return "fail", []                                 # a bucket fetch failed -> untrusted day

    rows = []
    for h in hours:
        ts_h = _local_ts(date, h, tz)
        asks_raw = [sb.get(ts_h) for sb in series_by_bucket]
        if sum(a is not None for a in asks_raw) < 2:
            continue                                      # genuinely too thin to define a modal
        asks = [a if a is not None else 0.0 for a in asks_raw]
        modal = max(range(len(asks)), key=lambda i: asks[i])
        adj = [i for i in (modal - 1, modal + 1) if 0 <= i < len(asks)]
        kept_adj = max(adj, key=lambda i: asks[i]) if adj else None
        sum_asks = asks[modal] + (asks[kept_adj] if kept_adj is not None else 0.0)
        rows.append({
            "date": date.normalize(), "hour": h, "win_idx": win_idx, "modal_idx": modal,
            "modal_ask": asks[modal], "top1": int(modal == win_idx),
            "top3": int(abs(modal - win_idx) <= 1), "wing_sum_asks": sum_asks,
            "wing_covers": int(win_idx in ([modal, kept_adj] if kept_adj is not None else [modal])),
        })
    return "ok", rows


async def analyze_series(client, sem, series, tz, hours, days_back):
    """Reconstruct per-(day,hour) accuracy rows for one series. Returns (df, n_fail).
    df has columns: series, date, hour, win_idx, modal_idx, modal_ask, top1, top3,
    wing_sum_asks, wing_covers. Reusable by the single-series CLI and the all-markets driver."""
    today = pd.Timestamp.now(tz=tz).normalize().tz_localize(None)
    dates = [today - pd.Timedelta(days=k) for k in range(1, days_back + 1)]
    results = await asyncio.gather(*[_day_row(client, sem, series, d, tz, hours) for d in dates])
    rows = [r for st, rr in results if st == "ok" for r in rr]
    n_fail = sum(1 for st, _ in results if st == "fail")
    df = pd.DataFrame(rows)
    if not df.empty:
        df.insert(0, "series", series)
    return df, n_fail


def print_series_table(df, series, kind, tz, days_back, n_fail, hours):
    if df.empty:
        print(f"No reconstructable settled days for {series} (last {days_back}d; {n_fail} fetch-failed).")
        return
    n_days = df["date"].nunique()
    print("=" * 74)
    print(f"MARKET ACCURACY  {series}  ({kind.upper()})  tz={tz}")
    print(f"{n_days} settled days reconstructed  (last {days_back} attempted; {n_fail} dropped to fetch-fail)")
    print("=" * 74)
    print(f"{'hour':>5} {'N':>4} {'Top-1':>7} {'Top-3':>7} {'modal_ask':>10} {'wing<0.90':>10} {'wing_cov':>9}")
    for h in hours:
        g = df[df["hour"] == h]
        if g.empty:
            print(f"{h:>5} {'--':>4}")
            continue
        afford = g["wing_sum_asks"] < 0.90
        cov = g.loc[afford, "wing_covers"].mean() if afford.any() else float("nan")
        print(f"{h:>5} {len(g):>4} {g['top1'].mean():>6.1%} {g['top3'].mean():>6.1%} "
              f"{g['modal_ask'].mean():>10.2f} {afford.mean():>9.0%} {(cov if cov == cov else 0):>8.0%}")
    print("\nTop-1 = market favorite bucket settled YES.  Top-3 = winner within favorite +/-1.")
    print("wing<0.90 = % days the 2-leg drop_lower_ask wing costs < $0.90.  wing_cov = of those, % winner inside wing.")


async def _amain(series, tz, kind, days_back, hours, out_path, concurrency):
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=API, timeout=25,
                                 headers={"User-Agent": "weather-alpha-accuracy/0.1"}) as client:
        df, n_fail = await analyze_series(client, sem, series, tz, hours, days_back)
    print_series_table(df, series, kind, tz, days_back, n_fail, hours)
    if out_path and not df.empty:
        df.to_parquet(out_path)
        print(f"\nper-(day,hour) rows -> {out_path}")


def _parse():
    p = argparse.ArgumentParser(description="Kalshi market accuracy by intraday anchor hour")
    p.add_argument("--series", required=True, help="e.g. KXHIGHCHI, KXLOWTNYC")
    p.add_argument("--tz", required=True, help="IANA tz of the settlement station, e.g. America/Chicago")
    p.add_argument("--kind", default="high", choices=["high", "low"])
    p.add_argument("--days-back", type=int, default=75)
    p.add_argument("--hours", type=int, nargs="+", default=[8, 9, 10, 11, 12, 13])
    p.add_argument("--concurrency", type=int, default=4, help="max in-flight requests (keep low; Kalshi 429s)")
    p.add_argument("--out", default=None, help="optional parquet of per-(day,hour) rows")
    return p.parse_args()


if __name__ == "__main__":
    a = _parse()
    asyncio.run(_amain(a.series, a.tz, a.kind, a.days_back, a.hours, a.out, a.concurrency))
