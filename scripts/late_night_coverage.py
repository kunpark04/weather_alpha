"""Late-night top-1 / top-2 settlement coverage (+ cost & edge) by 30-min snapshot, per city.

Thesis under test (user's): late at night the daily temp extremum is realized but the market
sometimes hasn't fully converged (favorite resting at ~85% instead of ~98%), so buying the
favorite then earns the convergence to $1 in a few hours. This script measures BOTH halves:

  * COVERAGE  = P(settled winner is the top-1 / top-2 highest-priced bucket at the snapshot).
  * COST      = the favorite's price at the snapshot (top-1) and the 2-leg ask-sum (top-2).
  * EDGE      = coverage - cost (gross), and net of the Kalshi taker fee. Coverage ALONE is not
                tradeable: if the price already equals the win rate there is no edge (lesson #13 /
                topk_by_hour_allcities note). The gap is the whole story.

Snapshots: 20:00, 20:30, 21:00, 21:30, 22:00, 22:30, 23:00, 23:30, 00:00 LOCAL (each city's tz);
00:00 is midnight at the END of the event day (next calendar day). At each snapshot a bucket's
price is its LAST TRADE <= snapshot (carry-forward — exactly the "stale resting favorite" the
thesis is about). Truth = Kalshi settlement_value (the bucket that settled 'yes'). N>=3 priced
buckets required to rank. Reads the deep backfill (KXHIGH* and KXLOWT* identically).

Usage:
  python scripts/late_night_coverage.py                       # all high + low series found
  python scripts/late_night_coverage.py --kind high           # only KXHIGH*
  python scripts/late_night_coverage.py --kind low            # only KXLOWT*
  python scripts/late_night_coverage.py --out data/late_night_coverage.parquet
"""
from __future__ import annotations

import argparse
import glob
import io
import sys
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from weather_alpha.fees import trade_fee_cents  # canonical Kalshi fee

# series -> (city label, IANA tz). HIGH mirrors scripts/modal_rank_multicity.CITY.
HIGH = {
    "KXHIGHNY": ("NYC", "America/New_York"), "KXHIGHCHI": ("Chicago", "America/Chicago"),
    "KXHIGHMIA": ("Miami", "America/New_York"), "KXHIGHAUS": ("Austin", "America/Chicago"),
    "KXHIGHDEN": ("Denver", "America/Denver"), "KXHIGHPHIL": ("Philadelphia", "America/New_York"),
    "KXHIGHLAX": ("Los Angeles", "America/Los_Angeles"), "KXHIGHTLV": ("Las Vegas", "America/Los_Angeles"),
    "KXHIGHTNOLA": ("New Orleans", "America/Chicago"), "KXHIGHTSEA": ("Seattle", "America/Los_Angeles"),
    "KXHIGHTSFO": ("San Francisco", "America/Los_Angeles"), "KXHIGHTDC": ("Washington DC", "America/New_York"),
    "KXHIGHTATL": ("Atlanta", "America/New_York"), "KXHIGHTMIN": ("Minneapolis", "America/Chicago"),
    "KXHIGHTPHX": ("Phoenix", "America/Phoenix"), "KXHIGHTBOS": ("Boston", "America/New_York"),
    "KXHIGHTDAL": ("Dallas", "America/Chicago"), "KXHIGHTOKC": ("Oklahoma City", "America/Chicago"),
    "KXHIGHTSATX": ("San Antonio", "America/Chicago"), "KXHIGHTHOU": ("Houston", "America/Chicago"),
}
LOW = {
    "KXLOWTCHI": ("Chicago", "America/Chicago"), "KXLOWTMIA": ("Miami", "America/New_York"),
    "KXLOWTAUS": ("Austin", "America/Chicago"), "KXLOWTDEN": ("Denver", "America/Denver"),
    "KXLOWTPHIL": ("Philadelphia", "America/New_York"), "KXLOWTLAX": ("Los Angeles", "America/Los_Angeles"),
    "KXLOWTLV": ("Las Vegas", "America/Los_Angeles"), "KXLOWTNOLA": ("New Orleans", "America/Chicago"),
    "KXLOWTSEA": ("Seattle", "America/Los_Angeles"), "KXLOWTSFO": ("San Francisco", "America/Los_Angeles"),
    "KXLOWTDC": ("Washington DC", "America/New_York"), "KXLOWTATL": ("Atlanta", "America/New_York"),
    "KXLOWTMIN": ("Minneapolis", "America/Chicago"), "KXLOWTPHX": ("Phoenix", "America/Phoenix"),
    "KXLOWTBOS": ("Boston", "America/New_York"), "KXLOWTDAL": ("Dallas", "America/Chicago"),
    "KXLOWTOKC": ("Oklahoma City", "America/Chicago"), "KXLOWTSATX": ("San Antonio", "America/Chicago"),
    "KXLOWTHOU": ("Houston", "America/Chicago"),
}
# (hh, mm); hh==24 -> 00:00 of the NEXT calendar day (midnight ending the event day)
SNAPS = [(20, 0), (20, 30), (21, 0), (21, 30), (22, 0), (22, 30), (23, 0), (23, 30), (24, 0)]


def snap_label(hh: int, mm: int) -> str:
    return "00:00" if hh == 24 else f"{hh:02d}:{mm:02d}"


def find_base() -> Path:
    for c in (_ROOT.parent / "data" / "weather" / "backfill", _ROOT / "data" / "backfill"):
        if c.is_dir():
            return c
    raise SystemExit("no backfill dir found")


def load_series(base: Path, series: str, since: str | None = None) -> pd.DataFrame:
    frames = []
    for zp in sorted(glob.glob(str(base / series / "*.zip"))):
        if since and Path(zp).stem < since:   # zip stem is the event-date YYYY-MM-DD; lexicographic == chronological
            continue
        try:
            z = zipfile.ZipFile(zp)
            frames.append(pd.read_parquet(io.BytesIO(z.read(z.namelist()[0]))))
        except (zipfile.BadZipFile, KeyError, OSError):
            continue
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.normalize()
    return df


def series_rows(base: Path, series: str, tz_name: str, since: str | None = None) -> list[dict]:
    """One row per (event_date, snapshot) with hit/cost fields. `since`='YYYY-MM-DD' filters event_date."""
    df = load_series(base, series, since)
    if df.empty:
        return []
    if since:
        df = df[df["event_date"] >= pd.Timestamp(since)]
        if df.empty:
            return []
    tz = ZoneInfo(tz_name)
    out: list[dict] = []
    for ev_date, day in df.groupby("event_date"):
        winners = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(winners) != 1:
            continue  # voided / not-yet-finalized day
        win = winners[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        if day.empty:
            continue
        for hh, mm in SNAPS:
            base_day = pd.Timestamp(ev_date.year, ev_date.month, ev_date.day)
            if hh == 24:
                local = pd.Timestamp(base_day.year, base_day.month, base_day.day, 0, 0, tz=tz) + pd.Timedelta(days=1)
            else:
                local = pd.Timestamp(base_day.year, base_day.month, base_day.day, hh, mm, tz=tz)
            snap_utc = local.tz_convert("UTC")
            pre = day[day["timestamp"] <= snap_utc]
            if pre.empty:
                continue
            last = pre.groupby("ticker")["yes_price_cents"].last()
            if last.size < 3:
                continue
            ranked = list(last.sort_values(ascending=False).index)
            p1 = float(last[ranked[0]]) / 100.0
            p2 = float(last[ranked[1]]) / 100.0
            out.append({
                "snap": snap_label(hh, mm),
                "hit1": win == ranked[0],
                "hit2": win in ranked[:2],
                "price1": p1,                 # favorite's price (cost of the 1-leg buy)
                "sum2": p1 + p2,              # 2-leg cover cost
                "fee1_c": trade_fee_cents(p1, 1),
            })
    return out


def aggregate(rows: list[dict], series: str, kind: str, label: str) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    r = pd.DataFrame(rows)
    g = r.groupby("snap").agg(
        n=("hit1", "size"), top1=("hit1", "mean"), top2=("hit2", "mean"),
        price1=("price1", "mean"), sum2=("sum2", "mean"), fee1_c=("fee1_c", "mean"),
    ).reset_index()
    order = {snap_label(h, m): i for i, (h, m) in enumerate(SNAPS)}
    g = g.sort_values("snap", key=lambda s: s.map(order)).reset_index(drop=True)
    g["gap1"] = g["top1"] - g["price1"]                       # gross top-1 edge per $1
    g["net1"] = g["gap1"] - g["fee1_c"] / 100.0              # net of taker fee
    g["gap2"] = g["top2"] - g["sum2"]                         # gross 2-leg cover edge
    g["series"], g["city"], g["kind"] = series, label, kind
    return g


def print_city(g: pd.DataFrame, label: str, series: str, tz: str) -> None:
    print(f"\n## {label} ({series})  {tz}")
    print(f"{'snap':>6}{'N':>5}{'top1':>7}{'top2':>7}{'price1':>8}{'gap1':>7}{'net1':>7}{'sum2':>7}{'gap2':>7}")
    for _, x in g.iterrows():
        print(f"{x['snap']:>6}{int(x['n']):>5}{x['top1']:>7.0%}{x['top2']:>7.0%}"
              f"{x['price1']:>8.2f}{x['gap1']:>+7.0%}{x['net1']:>+7.0%}{x['sum2']:>7.2f}{x['gap2']:>+7.0%}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", choices=["high", "low", "both"], default="both")
    ap.add_argument("--out", default="data/late_night_coverage.parquet")
    ap.add_argument("--base", default=None)
    ap.add_argument("--since", default=None, help="only event_date >= YYYY-MM-DD (e.g. match the low-temp window)")
    args = ap.parse_args(argv)
    base = Path(args.base) if args.base else find_base()
    print(f"base = {base} | kind = {args.kind} | since = {args.since or 'all'}")

    groups = []
    if args.kind in ("high", "both"):
        groups.append(("high", HIGH))
    if args.kind in ("low", "both"):
        groups.append(("low", LOW))

    all_g = []
    for kind, mp in groups:
        print("\n" + "=" * 80)
        print(f"{kind.upper()}-TEMP  —  top-1/top-2 coverage, cost (price1/sum2) & edge by snapshot")
        print("=" * 80)
        for series, (label, tz) in mp.items():
            if not (base / series).is_dir():
                continue
            g = aggregate(series_rows(base, series, tz, args.since), series, kind, label)
            if g.empty:
                continue
            print_city(g, label, series, tz)
            all_g.append(g)

    if not all_g:
        print("\nno usable series — backfill may not be present yet")
        return 1

    big = pd.concat(all_g, ignore_index=True)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    big.to_parquet(args.out, index=False)

    # pooled (N-weighted) by kind x snapshot
    for kind, _ in groups:
        sub = big[big["kind"] == kind]
        if sub.empty:
            continue
        order = {snap_label(h, m): i for i, (h, m) in enumerate(SNAPS)}
        rows = []
        for snap, s in sub.groupby("snap"):
            N = s["n"].sum()
            rows.append({
                "snap": snap, "N": int(N),
                "top1": (s["top1"] * s["n"]).sum() / N, "top2": (s["top2"] * s["n"]).sum() / N,
                "price1": (s["price1"] * s["n"]).sum() / N, "sum2": (s["sum2"] * s["n"]).sum() / N,
                "net1": (s["net1"] * s["n"]).sum() / N,
            })
        pg = pd.DataFrame(rows).sort_values("snap", key=lambda c: c.map(order))
        print("\n" + "=" * 64)
        print(f"POOLED {kind.upper()}-TEMP across {sub['series'].nunique()} cities — N-weighted")
        print("=" * 64)
        print(f"{'snap':>6}{'N':>7}{'top1':>7}{'top2':>7}{'price1':>8}{'gap1':>7}{'net1':>7}{'gap2':>7}")
        for _, x in pg.iterrows():
            print(f"{x['snap']:>6}{int(x['N']):>7}{x['top1']:>7.0%}{x['top2']:>7.0%}"
                  f"{x['price1']:>8.2f}{x['top1']-x['price1']:>+7.0%}{x['net1']:>+7.0%}{x['top2']-x['sum2']:>+7.0%}")

    print(f"\nwrote {args.out}  ({len(big)} city-snapshot rows)")
    print("\nREAD: gap1/gap2 = coverage - cost (gross edge per $1). net1 = gap1 - Kalshi taker fee.")
    print("A high top-k with gap~0 is NOT tradeable (price already matched the win rate). The")
    print("favorite price here is LAST TRADE, not the ask you'd pay -- gap1 is an UPPER bound on")
    print("real edge; the orderbook cross-check (high-temp only) uses the true ask.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
