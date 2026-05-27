"""Refresh KMDW data parquets to include the most recent observations.

For each source (METAR, TAF, ASOS 1-min, CLI):
  1. If parquet doesn't exist, fetch from configured START_DATE to now.
  2. If parquet exists, find latest timestamp/date, fetch the gap [latest - overlap, now],
     concatenate, dedupe, write back.

Overlap windows are deliberately wider than the cadence (METAR: 6h, ASOS: 1h, TAF: 12h,
CLI: 2d) so re-runs handle late SPECIs / corrections cleanly without dropping rows.

Idempotent and safe to run anytime.

Usage:
    python refresh_data.py                  # refresh all 4 sources
    python refresh_data.py metar            # refresh just METAR
    python refresh_data.py metar asos cli   # refresh subset
"""
import sys
import requests
import pandas as pd
import numpy as np
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone, timedelta
from metar import Metar

STATION        = "KMDW"
DATA_DIR       = Path("data")
DATA_DIR.mkdir(exist_ok=True)
START_DATE     = "01/01/2020"        # initial backfill range for METAR / TAF / ASOS
CLI_START_DATE = "01/01/2015"        # CLI extends earlier for the 5yr DOY climatology
TODAY          = datetime.now(timezone.utc)

# Re-fetch overlap windows: refresh starts (latest - overlap) so late-arriving
# SPECIs / corrections get picked up. Dedupe handles the duplicate rows.
OVERLAP = {
    "metar": pd.Timedelta(hours=6),
    "taf":   pd.Timedelta(hours=12),
    "asos":  pd.Timedelta(hours=1),
    "cli":   pd.Timedelta(days=2),
}


# ============================================================================
# Fetcher functions (extracted verbatim from model_v3.ipynb §1).
# Keep these in sync with the notebook if you change one or the other.
# ============================================================================

def _parse_metar(raw_metar):
    try:
        obs = Metar.Metar(raw_metar)
        return {
            "report_type":  obs.type,
            "mod":          obs.mod,
            "wdir":         obs.wind_dir.value() if obs.wind_dir else None,
            "wspd_kt":      obs.wind_speed.value("KT") if obs.wind_speed else None,
            "gust_kt":      obs.wind_gust.value("KT") if obs.wind_gust else None,
            "wdir_from":    obs.wind_dir_from.value() if obs.wind_dir_from else None,
            "wdir_to":      obs.wind_dir_to.value() if obs.wind_dir_to else None,
            "peak_wspd_kt": obs.wind_speed_peak.value("KT") if obs.wind_speed_peak else None,
            "peak_wdir":    obs.wind_dir_peak.value() if obs.wind_dir_peak else None,
            "vis_sm":       obs.vis.value("SM") if obs.vis else None,
            "wx":           " ".join("".join(p or "" for p in tup) for tup in obs.weather) if obs.weather else None,
            "sky":          " ".join(
                                f"{c}{int(h.value('FT'))//100:03d}{t or ''}" if h else f"{c}{t or ''}"
                                for c, h, t in obs.sky
                            ) if obs.sky else None,
            "temp_c":       obs.temp.value("C") if obs.temp else None,
            "dewp_c":       obs.dewpt.value("C") if obs.dewpt else None,
            "max6_c":       obs.max_temp_6hr.value("C") if obs.max_temp_6hr else None,
            "min6_c":       obs.min_temp_6hr.value("C") if obs.min_temp_6hr else None,
            "max24_c":      obs.max_temp_24hr.value("C") if obs.max_temp_24hr else None,
            "min24_c":      obs.min_temp_24hr.value("C") if obs.min_temp_24hr else None,
            "press_mb":     obs.press.value("MB") if obs.press else None,
            "slp_mb":       obs.press_sea_level.value("MB") if obs.press_sea_level else None,
            "p1hr_in":      obs.precip_1hr.value("IN") if obs.precip_1hr else None,
            "p3hr_in":      obs.precip_3hr.value("IN") if obs.precip_3hr else None,
            "p6hr_in":      obs.precip_6hr.value("IN") if obs.precip_6hr else None,
            "p24hr_in":     obs.precip_24hr.value("IN") if obs.precip_24hr else None,
            "snow_in":      obs.snowdepth.value("IN") if obs.snowdepth else None,
        }
    except Exception:
        return {}


def fetch_metar(start_date, end_date, station):
    start = pd.to_datetime(start_date, format="%m/%d/%Y")
    end   = pd.to_datetime(end_date,   format="%m/%d/%Y")
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={station}&data=metar"
        f"&year1={start.year}&month1={start.month}&day1={start.day}"
        f"&year2={end.year}&month2={end.month}&day2={end.day}"
        "&tz=UTC&format=onlycomma&missing=M"
        "&report_type=3&report_type=4"
    )
    raw_metar = pd.read_csv(BytesIO(requests.get(url).content))
    parsed_metar = raw_metar["metar"].apply(_parse_metar).apply(pd.Series)
    parsed_metar.insert(0, "timestamp", pd.to_datetime(raw_metar["valid"], utc=True))
    parsed_metar.insert(0, "station", station)
    for c in ["wdir", "wspd_kt", "gust_kt", "wdir_from", "wdir_to",
              "peak_wspd_kt", "peak_wdir", "snow_in"]:
        parsed_metar[c] = parsed_metar[c].astype("Int64")
    return parsed_metar


def fetch_taf(start_iso, end_iso, station):
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/taf.py"
        f"?station=K{station}&sts={start_iso}&ets={end_iso}&fmt=csv"
    )
    taf = pd.read_csv(BytesIO(requests.get(url).content), low_memory=False)
    for c in ["valid", "fx_valid", "fx_valid_end"]:
        taf[c] = pd.to_datetime(taf[c], utc=True)
    for c in ["sknt", "drct", "gust", "ws_level", "ws_drct", "ws_sknt"]:
        taf[c] = taf[c].astype("Int64")
    return taf


def fetch_asos_1min(start_date, end_date, station):
    start = pd.to_datetime(start_date, format="%m/%d/%Y")
    end   = pd.to_datetime(end_date,   format="%m/%d/%Y")
    bare  = station[1:] if station.startswith("K") else station
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos1min.py"
        f"?station[]={bare}"
        f"&year1={start.year}&month1={start.month}&day1={start.day}&hour1=0&minute1=0"
        f"&year2={end.year}&month2={end.month}&day2={end.day}&hour2=0&minute2=0"
        "&vars[]=tmpf&vars[]=dwpf&vars[]=sknt&vars[]=drct"
        "&vars[]=gust_sknt&vars[]=gust_drct&vars[]=ptype&vars[]=precip"
        "&sample=1min&what=download&tz=UTC"
    )
    raw_asos = pd.read_csv(BytesIO(requests.get(url).content), na_values=["M"], low_memory=False)
    asos = pd.DataFrame({
        "station":   station,
        "timestamp": pd.to_datetime(raw_asos["valid(UTC)"], utc=True),
        "temp_f":    raw_asos["tmpf"].astype("Int64"),
        "dewp_f":    raw_asos["dwpf"].astype("Int64"),
        "wdir":      raw_asos["drct"].astype("Int64"),
        "wspd_kt":   raw_asos["sknt"].astype("Int64"),
        "gust_wdir": raw_asos["gust_drct"].astype("Int64"),
        "gust_kt":   raw_asos["gust_sknt"].astype("Int64"),
        "ptype":     raw_asos["ptype"],
        "precip_in": raw_asos["precip"],
    })
    return asos


def fetch_cli(start_date, end_date, station):
    start = pd.to_datetime(start_date, format="%m/%d/%Y")
    end   = pd.to_datetime(end_date,   format="%m/%d/%Y")
    frames = []
    for year in range(start.year, end.year + 1):
        url = f"https://mesonet.agron.iastate.edu/json/cli.py?station={station}&year={year}"
        frames.append(pd.DataFrame(requests.get(url).json()["results"]))
    raw = pd.concat(frames, ignore_index=True)
    cli = pd.DataFrame({
        "station":    station,
        "date":       pd.to_datetime(raw["valid"]),
        "max_temp_f": pd.to_numeric(raw["high"], errors="coerce").astype("Int64"),
    })
    return cli[(cli["date"].dt.date >= start.date()) & (cli["date"].dt.date < end.date())].reset_index(drop=True)


# ============================================================================
# Refresh logic (incremental + idempotent)
# ============================================================================

def _refresh(name, ts_col, dedupe_cols, fetcher, initial_args, overlap, end_format):
    """Generic incremental refresh.

    Args:
    - name (str): parquet basename (without .parquet) -- e.g., 'metar_KMDW'.
    - ts_col (str): timestamp column to find the latest value.
    - dedupe_cols (list[str]): columns to dedupe on after concat.
    - fetcher (callable): function(start, end, station) -> DataFrame.
    - initial_args (tuple): (initial_start_date_str, station) used on first-ever fetch.
    - overlap (pd.Timedelta): how far before latest to start refresh.
    - end_format (str): "MMDDYYYY" for MM/DD/YYYY, "ISO" for YYYY-MM-DDTHH:MMZ.
    """
    path = DATA_DIR / f"{name}.parquet"
    initial_start, station = initial_args

    def _fmt(dt):
        if end_format == "MMDDYYYY":
            return dt.strftime("%m/%d/%Y")
        elif end_format == "ISO":
            return dt.strftime("%Y-%m-%dT%H:%MZ")
        raise ValueError(f"unknown end_format {end_format!r}")

    end_str = _fmt(TODAY + timedelta(days=1))   # exclusive upper bound; pad +1d

    if not path.exists():
        print(f"  [{name}] no cache, full fetch from {initial_start}...")
        df = fetcher(initial_start, end_str, station)
        df.to_parquet(path)
        print(f"  [{name}] wrote {len(df)} rows; latest = {df[ts_col].max()}")
        return df

    existing = pd.read_parquet(path)
    if len(existing) == 0:
        print(f"  [{name}] empty cache, full fetch from {initial_start}...")
        df = fetcher(initial_start, end_str, station)
        df.to_parquet(path)
        print(f"  [{name}] wrote {len(df)} rows; latest = {df[ts_col].max()}")
        return df

    latest = existing[ts_col].max()
    # Make tz-naive for date formatting
    if hasattr(latest, "tz_convert"):
        latest_utc = latest.tz_convert("UTC") if latest.tzinfo else pd.Timestamp(latest).tz_localize("UTC")
    else:
        latest_utc = pd.Timestamp(latest).tz_localize("UTC") if pd.Timestamp(latest).tzinfo is None else latest

    refresh_start = latest_utc - overlap
    start_str = _fmt(refresh_start)

    print(f"  [{name}] cached latest = {latest_utc}, fetching {start_str} -> {end_str}")
    new_df = fetcher(start_str, end_str, station)
    if len(new_df) == 0:
        print(f"  [{name}] no new rows returned; cache already up to date.")
        return existing

    combined = pd.concat([existing, new_df], ignore_index=True)
    before = len(combined)
    combined = combined.drop_duplicates(subset=dedupe_cols, keep="last")
    after = len(combined)
    combined = combined.sort_values(ts_col).reset_index(drop=True)
    combined.to_parquet(path)
    print(f"  [{name}] added {len(combined) - len(existing)} new rows  "
          f"(deduped {before - after}); total {len(combined)}; "
          f"latest = {combined[ts_col].max()}")
    return combined


def refresh_metar():
    return _refresh(
        name=f"metar_{STATION}",
        ts_col="timestamp",
        dedupe_cols=["timestamp", "report_type", "mod"],
        fetcher=fetch_metar,
        initial_args=(START_DATE, STATION),
        overlap=OVERLAP["metar"],
        end_format="MMDDYYYY",
    )


def refresh_taf():
    """TAF uses ISO date strings; the wrapper handles that via end_format='ISO'.
    Station arg is the 3-letter suffix (no leading K)."""
    station_suffix = STATION[1:] if STATION.startswith("K") else STATION
    return _refresh(
        name=f"taf_{STATION}",
        ts_col="valid",
        dedupe_cols=["valid", "fx_valid", "ftype", "raw"],
        fetcher=fetch_taf,
        initial_args=("2020-01-01T00:00Z", station_suffix),
        overlap=OVERLAP["taf"],
        end_format="ISO",
    )


def refresh_asos():
    return _refresh(
        name=f"asos_{STATION}",
        ts_col="timestamp",
        dedupe_cols=["timestamp"],
        fetcher=fetch_asos_1min,
        initial_args=(START_DATE, STATION),
        overlap=OVERLAP["asos"],
        end_format="MMDDYYYY",
    )


def refresh_cli():
    return _refresh(
        name=f"cli_{STATION}",
        ts_col="date",
        dedupe_cols=["date"],
        fetcher=fetch_cli,
        initial_args=(CLI_START_DATE, STATION),
        overlap=OVERLAP["cli"],
        end_format="MMDDYYYY",
    )


REFRESHERS = {
    "metar": refresh_metar,
    "taf":   refresh_taf,
    "asos":  refresh_asos,
    "cli":   refresh_cli,
}


def main():
    if len(sys.argv) > 1:
        which = [w.lower() for w in sys.argv[1:]]
        unknown = [w for w in which if w not in REFRESHERS]
        if unknown:
            raise SystemExit(f"Unknown source(s): {unknown}. Pick from {list(REFRESHERS.keys())}.")
    else:
        which = list(REFRESHERS.keys())

    print(f"Refreshing {which} as of {TODAY.isoformat()}")
    print(f"Data dir: {DATA_DIR.resolve()}\n")

    for name in which:
        print(f"--- {name} ---")
        try:
            REFRESHERS[name]()
        except Exception as e:
            print(f"  [{name}] FAILED: {type(e).__name__}: {e}")
        print()

    print("Done.")


if __name__ == "__main__":
    main()
