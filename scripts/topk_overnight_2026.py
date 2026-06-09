"""Overnight->noon anchor sweep, 2026 events, all cities: top-1/2/3 market-rank coverage.

Window: 10 PM previous night (local) through 12 PM noon (local) of the event date, HOURLY.
This is the pre-1PM window -- the existing topk_by_hour_allcities.py only sweeps 6AM..4PM, so
this fills in the overnight tail that precedes the production 1 PM anchor.

Reuses scripts/modal_rank_multicity conventions VERBATIM so results are comparable to
data/accuracy_by_anchor.csv:
  - truth = the bucket whose Kalshi settlement_value == 'yes' (no external station truth)
  - rank  = yes_price_cents of the LAST trade at-or-before the anchor (last-trade proxy)
  - top-k = unconditional P(settled winner is in the k highest-priced buckets at the anchor)
  - need >=3 priced buckets at the anchor, else that (event, anchor) is dropped

Restricts to event_date.year == 2026. Only reads 2026-*.zip per city: an event D's overnight
ticks (D-1 evening) live in event D's own zip (named by event_date), so this is complete.

CAVEAT printed below: overnight anchors keep only the events that already had >=3 buckets
trading by then -- a non-random (more-liquid) subsample -- so early-anchor rates are NOT
directly comparable to late-anchor rates where N approaches all events. N is shown everywhere.

Outputs: per-city + pooled tables to stdout, and a tidy CSV to data/topk_overnight_2026.csv.

Usage:
  python scripts/topk_overnight_2026.py                 # all cities under data/backfill/
  python scripts/topk_overnight_2026.py KXHIGHCHI KXHIGHNY
"""
from __future__ import annotations

import glob
import io
import sys
import zipfile
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.modal_rank_multicity import BACKFILL, CITY

_ROOT = Path(__file__).resolve().parent.parent
OUT_CSV = _ROOT / "data" / "topk_overnight_2026.csv"

# (day_offset_from_event_date, local_hour) from 10 PM prev night .. 12 PM noon, hourly.
ANCHORS = [(-1, 22), (-1, 23)] + [(0, h) for h in range(0, 13)]


def lbl(off: int, h: int) -> str:
    if off == -1:
        return f"{h - 12}P"          # 22 -> 10P, 23 -> 11P
    if h == 0:
        return "12A"
    if h == 12:
        return "12P"
    return f"{h}A"                    # 1A .. 11A


LABELS = [lbl(o, h) for o, h in ANCHORS]


def load_2026(series: str) -> pd.DataFrame:
    """Concat only the 2026-*.zip event files for one city (each zip = one event_date's tape)."""
    frames = []
    for zp in sorted(glob.glob(str(BACKFILL / series / "2026-*.zip"))):
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


def city_sweep(series: str) -> pd.DataFrame:
    """Per-anchor rows: off, h, label, n, top1, top2, top3 for one city (2026 events only)."""
    df = load_2026(series)
    if df.empty:
        return pd.DataFrame()
    df = df[df["event_date"].dt.year == 2026]
    if df.empty:
        return pd.DataFrame()
    tz = ZoneInfo(CITY[series][1])
    rows = []
    for ev_date, day in df.groupby("event_date"):
        winners = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(winners) != 1:
            continue
        win = winners[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        for off, h in ANCHORS:
            ad = ev_date + pd.Timedelta(days=off)
            naive = pd.Timestamp(ad.year, ad.month, ad.day, h)
            # DST-safe: 2 AM may be nonexistent (spring) or ambiguous (fall) for ~1 day/yr.
            anchor = naive.tz_localize(tz, ambiguous=True,
                                       nonexistent="shift_forward").tz_convert("UTC")
            pre = day[day["timestamp"] <= anchor]
            if pre.empty:
                continue
            last = pre.groupby("ticker")["yes_price_cents"].last()
            if last.size < 3:
                continue
            ranked = list(last.sort_values(ascending=False).index)
            rows.append({"off": off, "h": h,
                         "t1": win == ranked[0], "t2": win in ranked[:2], "t3": win in ranked[:3]})
    if not rows:
        return pd.DataFrame()
    r = pd.DataFrame(rows)
    g = (r.groupby(["off", "h"])
           .agg(n=("t1", "size"), top1=("t1", "mean"), top2=("t2", "mean"), top3=("t3", "mean"))
           .reset_index())
    g["label"] = [lbl(o, h) for o, h in zip(g["off"], g["h"])]
    return g


def _grid(big: pd.DataFrame, metric: str, cities: list[str]) -> None:
    """Print a cities x anchors grid for one top-k metric."""
    print(f"\n{'city':<16}" + "".join(L.rjust(6) for L in LABELS))
    for city in cities:
        sub = big[big["city"] == city].set_index("label")
        cells = ""
        for L in LABELS:
            cells += (f"{sub.loc[L, metric]:>6.0%}" if L in sub.index else f"{'-':>6}")
        print(f"{city:<16}{cells}")


def main(argv):
    series_list = [s for s in (argv or CITY) if (BACKFILL / s).is_dir()]
    all_g = []
    for s in series_list:
        g = city_sweep(s)
        if g.empty:
            print(f"## {CITY[s][0]} ({s}): no usable 2026 days")
            continue
        g["series"] = s
        g["city"] = CITY[s][0]
        all_g.append(g)
        print(f"\n## {CITY[s][0]} ({s}) -- {CITY[s][1]}")
        print(f"{'anchor':>7}{'N':>6}{'top1':>8}{'top2':>8}{'top3':>8}")
        for _, r in g.iterrows():
            star = "*" if (r["off"] == 0 and r["h"] == 12) else " "
            print(f"{r['label']:>7}{star}{int(r['n']):>5}"
                  f"{r['top1']:>8.0%}{r['top2']:>8.0%}{r['top3']:>8.0%}")

    if not all_g:
        print("no data"); return 1

    big = pd.concat(all_g, ignore_index=True)
    big.to_csv(OUT_CSV, index=False,
               columns=["city", "series", "off", "h", "label", "n", "top1", "top2", "top3"])

    # order cities by top-2 at noon (the latest, fullest-N anchor in the window)
    noon = big[big["label"] == "12P"].set_index("city")["top2"]
    cities = sorted(big["city"].unique(), key=lambda c: -noon.get(c, -1.0))

    for metric, title in [("top1", "TOP-1"), ("top2", "TOP-2"), ("top3", "TOP-3")]:
        print("\n" + "=" * 92)
        print(f"{title} coverage  --  cities x anchor (10PM prev night .. 12PM noon, local), 2026 events")
        print("=" * 92)
        _grid(big, metric, cities)

    # pooled (N-weighted) knee across all cities
    big["w1"] = big["top1"] * big["n"]
    big["w2"] = big["top2"] * big["n"]
    big["w3"] = big["top3"] * big["n"]
    pg = (big.groupby(["off", "h"])
            .agg(N=("n", "sum"), w1=("w1", "sum"), w2=("w2", "sum"), w3=("w3", "sum"))
            .reset_index())
    pg["label"] = [lbl(o, h) for o, h in zip(pg["off"], pg["h"])]
    print("\n" + "=" * 60)
    print(f"POOLED across {len(all_g)} cities (N-weighted) -- top-k by anchor")
    print("=" * 60)
    print(f"{'anchor':>7}{'N':>8}{'top1':>8}{'top2':>8}{'top3':>8}")
    for _, r in pg.iterrows():
        print(f"{r['label']:>7}{int(r['N']):>8}"
              f"{r['w1']/r['N']:>8.0%}{r['w2']/r['N']:>8.0%}{r['w3']/r['N']:>8.0%}")

    print("\nCAVEAT: overnight anchors keep only events that already had >=3 buckets trading by then")
    print("(a more-liquid subsample), so early N << late N and early rates are upward-selected.")
    print(f"\nFull tidy results -> {OUT_CSV.relative_to(_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
