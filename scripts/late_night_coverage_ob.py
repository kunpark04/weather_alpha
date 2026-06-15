"""Late-night coverage — REAL ASK cross-check from the 13-day orderbook tape (high-temp only).

The deep-backfill analysis (scripts/late_night_coverage.py) ranks/prices buckets by LAST TRADE, so
its "cost" has no spread — it overstates tradeable edge. The orderbook logger tape
(data/weather/orderbook, ~2026-06-01..now) records the SIMULTANEOUS yes_bid/yes_ask every 60s, so
here the favorite's COST is the actual yes_ask you'd lift at that minute. This is the only source
that can confirm the user's premise directly: "a resting favorite ask sitting at ~85% at 11 PM."

Per (city, event_date, snapshot 20:00..00:00 local): take the last poll <= snapshot, rank buckets
by `last` (fallback mid) to pick the favorite, and read the favorite's yes_ask as the buy cost.
Truth (the winning bucket) is joined from the deep backfill's settlement_value for that date.
Only high-temp series have a tape. N is small per city (~13 days) — read the POOLED rows.

Usage:
  python scripts/late_night_coverage_ob.py
  python scripts/late_night_coverage_ob.py --ob-base <dir> --out data/late_night_ob.parquet
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from weather_alpha.fees import trade_fee_cents
from scripts.late_night_coverage import HIGH, SNAPS, snap_label, find_base, load_series


def ob_base_default() -> Path:
    env = os.environ.get("OB_LOCAL_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    return _ROOT.parent / "data" / "weather" / "orderbook"


def _bid(x):
    """A bid price in [0,1] (0 = no bid, valid)."""
    try:
        v = float(x)
        return v if 0.0 <= v <= 1.0 else None
    except (TypeError, ValueError):
        return None


def _ask(x):
    """An ask price in (0,1]. 1.00 IS a valid ask (favorite offered at full price = no edge);
    excluding it would cherry-pick toward 'cheap stale favorite' and bias the result."""
    try:
        v = float(x)
        return v if 0.0 < v <= 1.0 else None
    except (TypeError, ValueError):
        return None


def read_ob_day(zp: Path) -> pd.DataFrame:
    """Flatten one <series>/<date>.zip of per-ticker jsonl polls into a tidy frame."""
    recs = []
    with zipfile.ZipFile(zp) as z:
        for n in z.namelist():
            if not n.endswith(".jsonl"):
                continue
            for ln in z.read(n).decode("utf-8", "replace").splitlines():
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                recs.append((r.get("ts"), r.get("ticker"), r.get("yes_bid"),
                             r.get("yes_ask"), r.get("last")))
    if not recs:
        return pd.DataFrame()
    df = pd.DataFrame(recs, columns=["ts", "ticker", "yes_bid", "yes_ask", "last"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    return df.dropna(subset=["ts"])


def winners_from_backfill(base: Path, series: str, dates: set[str]) -> dict:
    """event_date(str %Y-%m-%d) -> winning ticker, reading ONLY the backfill zips for `dates`."""
    out = {}
    for ds in dates:
        zp = base / series / f"{ds}.zip"
        if not zp.exists():
            continue
        try:
            z = zipfile.ZipFile(zp)
            day = pd.read_parquet(io.BytesIO(z.read(z.namelist()[0])))
        except (zipfile.BadZipFile, KeyError, OSError):
            continue
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) == 1:
            out[ds] = w[0]
    return out


def series_rows(ob_base: Path, bf_base: Path, series: str, tz_name: str) -> list[dict]:
    zips = sorted(glob.glob(str(ob_base / series / "*.zip")))
    win_map = winners_from_backfill(bf_base, series, {Path(z).stem for z in zips})
    tz = ZoneInfo(tz_name)
    rows = []
    for zp in zips:
        ds = Path(zp).stem  # event-date
        win = win_map.get(ds)
        if win is None:
            continue
        df = read_ob_day(Path(zp))
        if df.empty:
            continue
        df = df.sort_values("ts")
        y, m, d = int(ds[:4]), int(ds[5:7]), int(ds[8:10])
        for hh, mm in SNAPS:
            if hh == 24:
                local = pd.Timestamp(y, m, d, 0, 0, tz=tz) + pd.Timedelta(days=1)
            else:
                local = pd.Timestamp(y, m, d, hh, mm, tz=tz)
            snap_utc = local.tz_convert("UTC")
            pre = df[df["ts"] <= snap_utc]
            if pre.empty:
                continue
            last_poll = pre.groupby("ticker").last()

            def fair(r):
                b, a = _bid(r["yes_bid"]), _ask(r["yes_ask"])
                if b is not None and a is not None:
                    return (b + a) / 2.0
                lv = _bid(r["last"])
                if lv is not None:
                    return lv
                return a if a is not None else (b if b is not None else -1.0)

            last_poll = last_poll.assign(rank=last_poll.apply(fair, axis=1))
            ranked = last_poll[last_poll["rank"] >= 0].sort_values("rank", ascending=False)
            if len(ranked) < 3:
                continue
            fav = ranked.index[0]
            ask1 = _ask(ranked.iloc[0]["yes_ask"])
            if ask1 is None:
                rows.append({"snap": snap_label(hh, mm), "no_ask": True})  # favorite uncliftable
                continue
            a2 = _ask(ranked.iloc[1]["yes_ask"])
            lt1 = _bid(ranked.iloc[0]["last"])
            rows.append({
                "snap": snap_label(hh, mm), "no_ask": False,
                "hit1": fav == win, "hit2": win in list(ranked.index[:2]),
                "ask1": ask1, "asksum2": ask1 + (a2 if a2 is not None else ask1),
                "lt1": lt1, "spread1": (ask1 - lt1) if lt1 is not None else None,
                "fee1_c": trade_fee_cents(ask1, 1),
            })
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ob-base", default=None)
    ap.add_argument("--out", default="data/late_night_ob.parquet")
    args = ap.parse_args(argv)
    ob_base = Path(args.ob_base) if args.ob_base else ob_base_default()
    bf_base = find_base()
    print(f"ob_base = {ob_base}\nbf_base = {bf_base}")

    all_rows = []
    for series, (label, tz) in HIGH.items():
        if not (ob_base / series).is_dir():
            continue
        rows = series_rows(ob_base, bf_base, series, tz)
        for r in rows:
            r["series"], r["city"] = series, label
        all_rows.extend(rows)

    if not all_rows:
        print("no orderbook rows")
        return 1
    big = pd.DataFrame(all_rows)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    big.to_parquet(args.out, index=False)

    order = {snap_label(h, m): i for i, (h, m) in enumerate(SNAPS)}
    rows = []
    for snap, s in big.groupby("snap"):
        tot = len(s)
        buy = s[~s["no_ask"].fillna(False)]
        nb = len(buy)
        if nb == 0:
            continue
        rows.append({
            "snap": snap, "Ntot": tot, "Nbuy": nb, "noask_pct": 1.0 - nb / tot,
            "top1": buy["hit1"].mean(), "top2": buy["hit2"].mean(),
            "ask1": buy["ask1"].mean(), "asksum2": buy["asksum2"].mean(),
            "spread1": buy["spread1"].mean(skipna=True), "fee1_c": buy["fee1_c"].mean(),
        })
    pg = pd.DataFrame(rows).sort_values("snap", key=lambda c: c.map(order))
    ncities = big.loc[big["no_ask"] == False, "series"].nunique()
    print("\n" + "=" * 88)
    print(f"POOLED HIGH-TEMP (orderbook tape, REAL ASK) across {ncities} cities "
          f"-- favorite must have a liftable ask")
    print("=" * 88)
    print(f"{'snap':>6}{'Nbuy':>6}{'noask%':>7}{'top1':>7}{'top2':>7}{'ask1':>7}"
          f"{'gap1':>7}{'net1':>7}{'ask-lt':>7}{'gap2':>7}")
    for _, x in pg.iterrows():
        net1 = (x["top1"] - x["ask1"]) - x["fee1_c"] / 100.0
        sp = f"{x['spread1']:+.2f}" if pd.notna(x["spread1"]) else "  n/a"
        print(f"{x['snap']:>6}{int(x['Nbuy']):>6}{x['noask_pct']:>7.0%}{x['top1']:>7.0%}{x['top2']:>7.0%}"
              f"{x['ask1']:>7.2f}{x['top1']-x['ask1']:>+7.0%}{net1:>+7.0%}{sp:>7}{x['top2']-x['asksum2']:>+7.0%}")
    print(f"\nwrote {args.out}  ({len(big)} obs; {int((~big['no_ask'].fillna(False)).sum())} buyable)")
    print("gap1 = top1 coverage - favorite ASK (real cost). net1 also subtracts the taker fee.")
    print("noask% = share of snapshots where the favorite had NO liftable ask (uninvestable).")
    print("ask-lt = mean(ask - last_trade) on the favorite: how much last-trade UNDERSTATES the ask.")
    print("If gap1<=0 the resting favorite is NOT a free convergence -- you pay ~full price.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
