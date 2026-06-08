"""Multi-city cumulative top-k settlement hit-rate by MARKET RANK, swept across anchor hours.

Reads the per-city/day backfill zips (scripts/backfill_cities.py) for every city under
data/backfill/, and for each anchor hour h (6 AM .. 4 PM in THAT CITY's local time) measures
how often the bucket that actually settled fell in the top-1 / top-2 / top-3 highest-priced
buckets at the anchor. Market-only (no model/agreement).

Truth is taken DIRECTLY from Kalshi's own settlement: the market whose settlement_value == 'yes'
is the winning bucket. No external CLI/station truth, so no station-mapping or basis risk.

Ranking uses yes_price_cents (last trade <= anchor) as the market's implied value per bucket
-- the same last-trade proxy the single-city analysis uses; the +/-1c ask cap is irrelevant to
rank order, so it is omitted.

Usage:
  python scripts/modal_rank_multicity.py                 # all cities found in data/backfill/
  python scripts/modal_rank_multicity.py KXHIGHNY KXHIGHCHI
"""
from __future__ import annotations

import glob
import io
import sys
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
BACKFILL = _ROOT / "data" / "backfill"
HOURS = range(6, 17)   # 6 AM .. 4 PM local

# series -> (city label, IANA tz of its 1 PM anchor)
CITY = {
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


def load_city(series: str) -> pd.DataFrame:
    frames = []
    for zp in sorted(glob.glob(str(BACKFILL / series / "*.zip"))):
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


def city_hourly(series: str) -> pd.DataFrame:
    """Return per-hour rows: hour, n, top1, top2, top3 for one city."""
    df = load_city(series)
    if df.empty:
        return pd.DataFrame()
    tz = ZoneInfo(CITY[series][1])
    rows = []
    for ev_date, day in df.groupby("event_date"):
        # winning bucket = the market that settled 'yes'
        winners = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(winners) != 1:
            continue
        win = winners[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        for h in HOURS:
            anchor = pd.Timestamp(ev_date.year, ev_date.month, ev_date.day, h, tz=tz).tz_convert("UTC")
            pre = day[day["timestamp"] <= anchor]
            if pre.empty:
                continue
            last = pre.groupby("ticker")["yes_price_cents"].last()
            if last.size < 3:
                continue
            ranked = list(last.sort_values(ascending=False).index)
            rows.append({
                "hour": h,
                "t1": win == ranked[0],
                "t2": win in ranked[:2],
                "t3": win in ranked[:3],
            })
    if not rows:
        return pd.DataFrame()
    r = pd.DataFrame(rows)
    g = r.groupby("hour").agg(n=("t1", "size"), top1=("t1", "mean"),
                              top2=("t2", "mean"), top3=("t3", "mean")).reset_index()
    return g


def main(argv):
    series_list = argv or [s for s in CITY if (BACKFILL / s).is_dir()]
    series_list = [s for s in series_list if (BACKFILL / s).is_dir()]
    all_rows = []
    print("=" * 78)
    print("Multi-city cumulative top-k settlement hit-rate by MARKET rank, by anchor hour")
    print("  (settles in top-k highest-priced buckets; truth = Kalshi settlement_value)")
    print("=" * 78)
    for s in series_list:
        g = city_hourly(s)
        city = CITY[s][0]
        if g.empty:
            print(f"\n## {city} ({s}): no usable days yet")
            continue
        print(f"\n## {city} ({s})  -- {CITY[s][1]}")
        print(f"{'hr':>4}{'N':>6}{'top1':>8}{'top2':>8}{'top3':>8}")
        for _, row in g.iterrows():
            star = "*" if row["hour"] == 13 else " "
            print(f"{int(row['hour']):>3}{star}{int(row['n']):>6}{row['top1']:>8.0%}{row['top2']:>8.0%}{row['top3']:>8.0%}")
        g["series"] = s
        all_rows.append(g)

    if all_rows:
        big = pd.concat(all_rows, ignore_index=True)
        big["city"] = big["series"].map(lambda s: CITY[s][0])
        labels = {13: "1 PM", 14: "2 PM", 15: "3 PM", 16: "4 PM"}
        for h in (13, 14, 15, 16):
            sub = big[big["hour"] == h].sort_values("top2", ascending=False)
            if sub.empty:
                continue
            print("\n" + "=" * 52)
            print(f"{labels[h]} anchor (each city's local time) -- ranked by top-2")
            print("=" * 52)
            print(f"{'city':<16}{'N':>5}{'top1':>8}{'top2':>8}{'top3':>8}")
            for _, r in sub.iterrows():
                print(f"{r['city']:<16}{int(r['n']):>5}{r['top1']:>8.0%}{r['top2']:>8.0%}{r['top3']:>8.0%}")

    if all_rows:
        pooled = pd.concat(all_rows, ignore_index=True)
        # weight each city-hour by its N for a pooled rate
        pooled["w1"] = pooled["top1"] * pooled["n"]
        pooled["w2"] = pooled["top2"] * pooled["n"]
        pooled["w3"] = pooled["top3"] * pooled["n"]
        pg = pooled.groupby("hour").agg(N=("n", "sum"), w1=("w1", "sum"),
                                        w2=("w2", "sum"), w3=("w3", "sum")).reset_index()
        print("\n" + "=" * 60)
        print(f"POOLED across {len(all_rows)} cities -- top-k by anchor hour")
        print("=" * 60)
        print(f"{'hr':>4}{'N':>7}{'top1':>8}{'top2':>8}{'top3':>8}")
        for _, row in pg.iterrows():
            star = "*" if row["hour"] == 13 else " "
            print(f"{int(row['hour']):>3}{star}{int(row['N']):>7}"
                  f"{row['w1']/row['N']:>8.0%}{row['w2']/row['N']:>8.0%}{row['w3']/row['N']:>8.0%}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
