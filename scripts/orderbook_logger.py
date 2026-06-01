"""Always-on Kalshi orderbook logger for daily-high series (Chicago KXHIGHCHI + Houston KXHIGHTHOU).

Polls PUBLIC (no-auth) Kalshi market-data every `--interval` seconds and logs the
full order book + top-of-book for EVERY currently-open market in each configured series — handling
the case where TWO event-days are open at once (verified live: 26MAY29 + 26MAY30).

  - GET /markets?series_ticker=KXHIGHCHI&status=open  (paginated) -> all open markets
  - GET /markets/{ticker}/orderbook                                -> full depth ladder

Each market is filed under its OWN event date (parsed from the event_ticker), so
overlapping days never mix:
  <out>/<SERIES>/<EVENT-DATE>/<ticker>.jsonl     e.g. data/orderbook/KXHIGHCHI/2026-05-29/KXHIGHCHI-26MAY29-T80.jsonl

When an event SETTLES (its markets drop out of the open set) and its date is in the
past, that day's folder is zipped to <out>/<EVENT-DATE>.zip and the raw folder removed.
So you get one self-contained zip per trading day: 2026-05-29.zip, 2026-05-30.zip, …

RELIABILITY (so it won't silently stop like before):
  - Every poll cycle is wrapped in try/except — a network blip / API 5xx / parse error
    logs a WARNING and the loop continues. No single cycle can kill the process.
  - Writes are flushed + fsync'd; JSONL is append-only (a hard kill loses <= 1 line).
  - The zip trigger is settlement-based (open-set membership) + past-date guarded, so a
    transient API hiccup can't prematurely zip a still-active or overlapping day.
  - Run under a supervisor on an ALWAYS-ON host (systemd Restart=always — see
    deploy/orderbook-logger.service) so a crash OR host reboot auto-restarts it. Your
    laptop being off is the one thing code can't fix; host it on a VM/Pi.

Zero third-party deps (stdlib only). Needs IANA tz for America/Chicago: native on
Linux; on Windows run `pip install tzdata` once.

Usage:
  python scripts/orderbook_logger.py                 # 60s cadence -> ./data/orderbook
  python scripts/orderbook_logger.py --interval 300  # 5-min
  python scripts/orderbook_logger.py --once          # one cycle then exit (test)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.request
import urllib.error
import zipfile
from datetime import datetime, timezone, date
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
    CHICAGO = ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover
    CHICAGO = timezone.utc

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = ["KXHIGHCHI", "KXHIGHTHOU"]   # series to log (Chicago + Houston); override with --series
UA = {"User-Agent": "weather-alpha-orderbook-logger/1.0", "Accept": "application/json"}

log = logging.getLogger("orderbook_logger")
_STOP = False


def _http_json(url: str, timeout: float = 20.0) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def fetch_open_markets() -> list[dict]:
    """All currently-open markets across every open event in each configured series (paginated)."""
    out: list[dict] = []
    for series in SERIES:
        cursor = ""
        for _ in range(20):  # hard page cap (safety), per series
            url = f"{BASE}/markets?series_ticker={series}&status=open&limit=100"
            if cursor:
                url += f"&cursor={cursor}"
            j = _http_json(url)
            out.extend(j.get("markets", []))
            cursor = j.get("cursor") or ""
            if not cursor:
                break
    return out


def event_date_from_ticker(event_ticker: str) -> date | None:
    """KXHIGHCHI-26MAY29 -> date(2026, 5, 29)."""
    try:
        return datetime.strptime(event_ticker.split("-", 1)[1], "%y%b%d").date()
    except Exception:
        return None


def build_record(ts: str, m: dict) -> dict:
    tkr = m["ticker"]
    rec = {
        "ts": ts, "event": m.get("event_ticker"), "ticker": tkr,
        "subtitle": m.get("subtitle"), "status": m.get("status"),
        "yes_bid": m.get("yes_bid_dollars"), "yes_ask": m.get("yes_ask_dollars"),
        "yes_bid_size": m.get("yes_bid_size_fp"), "yes_ask_size": m.get("yes_ask_size_fp"),
        "no_bid": m.get("no_bid_dollars"), "no_ask": m.get("no_ask_dollars"),
        "last": m.get("last_price_dollars"), "vol": m.get("volume_fp"),
        "vol_24h": m.get("volume_24h_fp"), "open_interest": m.get("open_interest_fp"),
        "liquidity": m.get("liquidity_dollars"),
    }
    try:
        ob = _http_json(f"{BASE}/markets/{tkr}/orderbook").get("orderbook_fp", {}) or {}
        rec["yes_book"] = ob.get("yes_dollars")   # [[price, qty], ...] | None
        rec["no_book"] = ob.get("no_dollars")
    except Exception as e:
        rec["orderbook_err"] = repr(e)[:160]
    return rec


def poll() -> tuple[dict[str, list[dict]], set[str]]:
    """Return ({event-date-str: [records]}, {open event-date-strs})."""
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    by_date: dict[str, list[dict]] = {}
    open_dates: set[str] = set()
    for m in fetch_open_markets():
        ed = event_date_from_ticker(m.get("event_ticker", ""))
        if ed is None:
            continue
        ds = ed.strftime("%Y-%m-%d")
        open_dates.add(ds)
        by_date.setdefault(ds, []).append(build_record(ts, m))
    return by_date, open_dates


def append_records(out: Path, ds: str, records: list[dict]) -> None:
    """Append each market's snapshot to <out>/<series>/<ds>/<ticker>.jsonl — one file per market,
    grouped by SERIES (city) first so each city's data + zips stay fully separate."""
    by_ticker: dict[str, list[dict]] = {}
    for r in records:
        by_ticker.setdefault(r["ticker"], []).append(r)
    for tkr, recs in by_ticker.items():
        series = tkr.split("-", 1)[0]                       # KXHIGHCHI-26JUN01-B72.5 -> KXHIGHCHI
        day_dir = out / series / ds
        day_dir.mkdir(parents=True, exist_ok=True)
        with open(day_dir / f"{tkr}.jsonl", "a", encoding="utf-8") as f:
            for r in recs:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
            f.flush()
            os.fsync(f.fileno())


def zip_settled_days(out: Path, open_dates: set[str], today: date) -> None:
    """Zip+remove any <series>/<day> folder that is NOT currently open AND whose date is past —
    one zip PER CITY PER DAY at <out>/<series>/<date>.zip (so each city stays separate)."""
    for series_dir in sorted(p for p in out.iterdir() if p.is_dir()):
        for folder in sorted(p for p in series_dir.iterdir() if p.is_dir()):
            try:
                fdate = datetime.strptime(folder.name, "%Y-%m-%d").date()
            except ValueError:
                continue
            if folder.name in open_dates or fdate >= today:
                continue  # still trading, or today/future — leave it
            zpath = series_dir / f"{folder.name}.zip"
            try:
                with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
                    for fp in sorted(folder.rglob("*")):
                        if fp.is_file():
                            z.write(fp, arcname=str(fp.relative_to(folder)))
                for fp in sorted(folder.rglob("*"), reverse=True):
                    fp.unlink() if fp.is_file() else fp.rmdir()
                folder.rmdir()
                log.info("settled+zipped %s/%s -> %s/%s", series_dir.name, folder.name,
                         series_dir.name, zpath.name)
            except Exception:
                log.exception("zip failed for %s/%s (left raw folder)", series_dir.name, folder.name)


def _handle_stop(signum, frame):
    global _STOP
    _STOP = True
    log.info("signal %s — finishing cycle then exiting", signum)


def main(argv=None) -> int:
    global SERIES
    ap = argparse.ArgumentParser(description="Always-on Kalshi daily-high orderbook logger")
    ap.add_argument("--interval", type=float, default=60.0, help="seconds between polls (default 60)")
    ap.add_argument("--out", default="data/orderbook", help="output base dir")
    ap.add_argument("--series", default=",".join(SERIES),
                    help="comma-separated Kalshi series to log (default: %(default)s)")
    ap.add_argument("--once", action="store_true", help="single cycle then exit (test)")
    args = ap.parse_args(argv)
    SERIES = [s.strip() for s in args.series.split(",") if s.strip()]

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)

    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    log.info("orderbook logger up | series=%s | interval=%ss | out=%s | tz=%s",
             ",".join(SERIES), args.interval, out, CHICAGO)

    while not _STOP:
        t0 = time.monotonic()
        today = datetime.now(CHICAGO).date()
        try:
            by_date, open_dates = poll()
            for ds, recs in by_date.items():
                append_records(out, ds, recs)
            if by_date:
                log.info("logged %d markets across %d event(s): %s",
                         sum(len(v) for v in by_date.values()), len(by_date), sorted(by_date))
            else:
                log.warning("no open markets for %s right now", ",".join(SERIES))
            zip_settled_days(out, open_dates, today)
        except urllib.error.HTTPError as e:
            log.warning("HTTP %s polling Kalshi: %s", e.code, e.reason)
        except Exception as e:
            log.warning("poll cycle failed (continuing): %r", e)

        if args.once:
            break
        remaining = max(0.0, args.interval - (time.monotonic() - t0))
        slept = 0.0
        while slept < remaining and not _STOP:
            time.sleep(min(1.0, remaining - slept))
            slept += 1.0
    log.info("logger stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
