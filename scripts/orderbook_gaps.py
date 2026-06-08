#!/usr/bin/env python3
"""Diagnose GAPS + COVERAGE in the orderbook logger output.

The logger (`scripts/orderbook_logger.py`) stamps every record in a 60s poll cycle with the
SAME `ts` (UTC, seconds), shared across all of that day's tickers. So the set of DISTINCT `ts`
for a (series, event-date) IS that day's poll timeline — holes in it are logger downtime.

Scans both shapes the data takes:
  <base>/<SERIES>/<DATE>.zip   (settled day, pulled to the laptop)
  <base>/<SERIES>/<DATE>/      (raw, still-open day on the droplet)

and reports per (series, event-date): #polls, span, expected polls (span/interval), coverage%,
and the largest intra-day gap; plus per-date SERIES COVERAGE (how many series logged that day,
to catch "15/20" type shortfalls). Note event-date folders span ~26h: a daily-high market opens
~the prior day ~14:00 UTC and settles the next, and two event-days overlap — so a full healthy
day is ~1500+ polls, not 1440.

Usage:
  python scripts/orderbook_gaps.py [--base DIR] [--interval 60] [--gap 180] [--expect 20]
"""
from __future__ import annotations

import argparse
import os
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _ts_epochs(lines) -> set[float]:
    out: set[float] = set()
    for ln in lines:
        i = ln.find('"ts":"')
        if i < 0:
            continue
        j = ln.find('"', i + 6)
        if j < 0:
            continue
        try:
            out.add(datetime.fromisoformat(ln[i + 6:j]).timestamp())
        except ValueError:
            pass
    return out


def _day_ts(base: Path, series: str, date: str) -> list[float]:
    ts: set[float] = set()
    z = base / series / f"{date}.zip"
    raw = base / series / date
    if z.exists():
        with zipfile.ZipFile(z) as zf:
            for n in zf.namelist():
                if n.endswith(".jsonl"):
                    ts |= _ts_epochs(zf.read(n).decode("utf-8", "replace").splitlines())
    if raw.is_dir():
        for fp in raw.glob("*.jsonl"):
            ts |= _ts_epochs(fp.read_text("utf-8", "replace").splitlines())
    return sorted(ts)


def _fmt(ep: float) -> str:
    return datetime.fromtimestamp(ep, tz=timezone.utc).strftime("%m-%dT%H:%M")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Diagnose orderbook logger gaps + coverage")
    ap.add_argument("--base", default=os.environ.get("OB_LOCAL_DIR"))
    ap.add_argument("--interval", type=float, default=60.0)
    ap.add_argument("--gap", type=float, default=180.0, help="flag gaps larger than this (seconds)")
    ap.add_argument("--expect", type=int, default=20, help="expected series count on a mature day")
    a = ap.parse_args(argv)

    base = Path(a.base) if a.base else (Path(__file__).resolve().parents[1].parent / "data" / "weather" / "orderbook")
    if not base.is_dir():
        print(f"base not found: {base}")
        return 2

    entries: list[tuple[str, str]] = []
    per_date: dict[str, set[str]] = defaultdict(set)
    for sd in sorted(p for p in base.iterdir() if p.is_dir()):
        dates = {z.stem for z in sd.glob("*.zip")} | {d.name for d in sd.iterdir() if d.is_dir()}
        for dt in dates:
            entries.append((dt, sd.name))
            per_date[dt].add(sd.name)

    print(f"base = {base}\n")
    print(f"{'event-date':11} {'series':13} {'polls':>6} {'exp':>6} {'cov%':>6} {'maxgap':>7}  span(UTC)")
    flagged: list[tuple] = []
    for dt, series in sorted(entries):
        ts = _day_ts(base, series, dt)
        if not ts:
            continue
        span = ts[-1] - ts[0]
        exp = int(round(span / a.interval)) + 1
        cov = 100.0 * len(ts) / exp if exp else 0.0
        deltas = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
        maxgap = max(deltas) if deltas else 0.0
        ngaps = sum(1 for d in deltas if d > a.gap)
        flag = ""
        if cov < 95.0 or maxgap > a.gap:
            flag = f"  <-- {ngaps} gap(s) > {int(a.gap)}s"
            flagged.append((dt, series, cov, maxgap, ngaps))
        print(f"{dt:11} {series:13} {len(ts):6d} {exp:6d} {cov:6.1f} {int(maxgap):7d}  {_fmt(ts[0])}..{_fmt(ts[-1])}{flag}")

    print(f"\n=== series logged per event-date (expect {a.expect} on a mature day) ===")
    for dt in sorted(per_date):
        n = len(per_date[dt])
        print(f"{dt}: {n:2d} series" + ("" if n >= a.expect else f"   <-- only {n}/{a.expect}"))

    print("\n=== GAP FLAGS (coverage < 95% or a gap > threshold) ===")
    if flagged:
        for dt, series, cov, mg, ng in sorted(flagged):
            print(f"  {dt} {series}: cov={cov:.1f}%  maxgap={int(mg)}s  ({ng} gaps)")
    else:
        print(f"  none — every day-file is >=95% coverage with no gap > {int(a.gap)}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
