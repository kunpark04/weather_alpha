"""Real-time data fetchers — in-process, low-latency replacements for the subprocess
wrappers around refresh_data.py / backfill_hrrr.py.

Ported from notebooks/live.ipynb §2.x with two production additions:

  * `fetch_live_asos_synoptic()` — NEW. Uses Synoptic's free Mesonet API for
    sub-minute ASOS, eliminating IEM's 24–48 h lag (HANDOFF §5.3). Falls back
    to IEM when no SYNOPTIC_TOKEN is configured.
  * Async orchestrator `fetch_all_live()` — runs all sources in parallel via
    asyncio.to_thread + asyncio.gather. Total wall time ~3–5 s.

The fetchers return DataFrames in the **same schema** as the historical parquets
(METAR/TAF/ASOS/CLI), so downstream merging into the existing parquet store works
without schema fixups. HRRR is returned in the wide format that
`backfill_hrrr.py` writes (one row per (date, fxx) with columns per variable).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests

# Re-use the historical fetchers from refresh_data.py for IEM-backed sources
# (TAF / CLI / IEM-ASOS fallback). This guarantees schema parity with training.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from refresh_data import (  # noqa: E402
    _parse_metar,
    fetch_asos_1min as _hist_fetch_asos_1min,
    fetch_cli as _hist_fetch_cli,
    fetch_taf as _hist_fetch_taf,
)

logger = logging.getLogger(__name__)

WEATHER_GOV = "https://api.weather.gov"
SYNOPTIC_API = "https://api.synopticdata.com/v2/stations/timeseries"
USER_AGENT = "forecast-alpha/0.1 (kunpark04@gmail.com)"


# ---------------------------------------------------------------------------
# Schema parity helper
# ---------------------------------------------------------------------------

def coerce_to_reference(df: pd.DataFrame, reference_parquet: Path) -> pd.DataFrame:
    """Cast columns to match the historical parquet's dtypes. Idempotent.

    Live samples can be small enough that pandas infers `object` for all-None
    columns; this guarantees dtype parity with the trained model's inputs.
    """
    try:
        ref = pd.read_parquet(reference_parquet)
    except FileNotFoundError:
        return df
    for col in df.columns:
        if col not in ref.columns or df[col].dtype == ref[col].dtype:
            continue
        try:
            df[col] = df[col].astype(ref[col].dtype)
        except (ValueError, TypeError):
            pass
    return df


# ---------------------------------------------------------------------------
# METAR — api.weather.gov (sub-minute latency)
# ---------------------------------------------------------------------------

def _parse_api_props(props: dict) -> dict:
    """Extract METAR-equivalent fields from api.weather.gov's properties dict.

    Used for 5-min observations that lack rawMessage. Schema matches
    refresh_data.fetch_metar() so downstream code is unchanged.
    """
    def _v(k):
        d = props.get(k)
        return d.get("value") if isinstance(d, dict) else None

    def _kmh_to_kt(v):  return None if v is None else round(v * 0.539957)
    def _m_to_sm(v):    return None if v is None else v / 1609.344
    def _pa_to_mb(v):   return None if v is None else v / 100.0

    return {
        "report_type":  None, "mod": None,
        "wdir":         _v("windDirection"),
        "wspd_kt":      _kmh_to_kt(_v("windSpeed")),
        "gust_kt":      _kmh_to_kt(_v("windGust")),
        "wdir_from":    None, "wdir_to": None,
        "peak_wspd_kt": None, "peak_wdir": None,
        "vis_sm":       _m_to_sm(_v("visibility")),
        "wx":           None, "sky": None,
        "temp_c":       _v("temperature"),
        "dewp_c":       _v("dewpoint"),
        "max6_c":       None, "min6_c": None, "max24_c": None, "min24_c": None,
        "press_mb":     _pa_to_mb(_v("barometricPressure")),
        "slp_mb":       _pa_to_mb(_v("seaLevelPressure")),
        "p1hr_in":      None, "p3hr_in": None, "p6hr_in": None, "p24hr_in": None,
        "snow_in":      None,
    }


def fetch_live_metar(station: str, *, limit: int = 40, data_dir: Path | None = None) -> pd.DataFrame:
    """All recent observations from api.weather.gov (sub-minute latency).

    Combines hourly METAR/SPECI rows (parsed via metar.Metar at 0.1 °C precision)
    with the in-between 5-min auto obs (parsed from API properties). Returns the
    unified DataFrame; downstream code can filter on `report_type` if needed.
    """
    url = f"{WEATHER_GOV}/stations/{station}/observations?limit={limit}"
    resp = requests.get(url, headers={"User-Agent": USER_AGENT,
                                      "Accept": "application/geo+json"}, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    rows = []
    for feat in payload.get("features", []):
        props = feat.get("properties", {})
        ts = pd.to_datetime(props["timestamp"], utc=True)
        raw = props.get("rawMessage")
        parsed = _parse_metar(raw) if raw else _parse_api_props(props)
        rows.append({"station": station, "timestamp": ts, **parsed})

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for c in ["wdir", "wspd_kt", "gust_kt", "wdir_from", "wdir_to",
              "peak_wspd_kt", "peak_wdir", "snow_in"]:
        if c in df.columns:
            df[c] = df[c].astype("Int64")
    df = df.sort_values("timestamp").reset_index(drop=True)
    if data_dir is not None:
        df = coerce_to_reference(df, data_dir / f"metar_{station}.parquet")
    return df


# ---------------------------------------------------------------------------
# TAF / CLI — IEM (no real-time alternative needed at our cadence)
# ---------------------------------------------------------------------------

def fetch_live_taf(station: str, *, hours_back: int = 36,
                   data_dir: Path | None = None) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)
    bare = station[1:] if station.startswith("K") else station
    df = _hist_fetch_taf(start.strftime("%Y-%m-%dT%H:%MZ"),
                         now.strftime("%Y-%m-%dT%H:%MZ"), bare)
    if data_dir is not None:
        df = coerce_to_reference(df, data_dir / f"taf_{station}.parquet")
    return df


def fetch_live_cli(station: str, *, days_back: int = 30,
                   data_dir: Path | None = None) -> pd.DataFrame:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back)
    df = _hist_fetch_cli(start.strftime("%m/%d/%Y"),
                         (now + timedelta(days=1)).strftime("%m/%d/%Y"), station)
    if data_dir is not None:
        df = coerce_to_reference(df, data_dir / f"cli_{station}.parquet")
    return df


# ---------------------------------------------------------------------------
# ASOS — Synoptic (real-time) with IEM fallback
# ---------------------------------------------------------------------------

def fetch_live_asos_iem(station: str, *, hours_back: int = 48,
                        data_dir: Path | None = None) -> pd.DataFrame:
    """IEM ASOS 1-min — same source as historical parquet. ~24-48 h lag."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)
    df = _hist_fetch_asos_1min(start.strftime("%m/%d/%Y"),
                               (now + timedelta(days=1)).strftime("%m/%d/%Y"), station)
    if data_dir is not None:
        df = coerce_to_reference(df, data_dir / f"asos_{station}.parquet")
    return df


def fetch_live_asos_synoptic(station: str, token: str, *, hours_back: int = 48,
                             data_dir: Path | None = None) -> pd.DataFrame:
    """Real-time 1-min ASOS via Synoptic's Mesonet API (free tier suffices).

    Returns a DataFrame matching the IEM ASOS parquet schema:
        station, timestamp, temp_f, dewp_f, wdir, wspd_kt, gust_wdir, gust_kt,
        ptype, precip_in.
    """
    stid = station[1:] if station.startswith("K") else station
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=hours_back)
    params = {
        "token":       token,
        "stid":        stid,
        "start":       start.strftime("%Y%m%d%H%M"),
        "end":         now.strftime("%Y%m%d%H%M"),
        "vars":        "air_temp,dew_point_temperature,wind_speed,wind_direction,wind_gust",
        "obtimezone":  "utc",
        "units":       "english",   # gets °F, knots, inches — matches IEM schema
    }
    resp = requests.get(SYNOPTIC_API, params=params, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    stations = payload.get("STATION", [])
    if not stations:
        logger.warning("Synoptic returned no station data for %s", station)
        return pd.DataFrame()

    obs = stations[0].get("OBSERVATIONS", {})
    times = obs.get("date_time", [])
    if not times:
        return pd.DataFrame()

    def _g(key):
        return obs.get(key, [None] * len(times))

    df = pd.DataFrame({
        "station":   station,
        "timestamp": pd.to_datetime(times, utc=True),
        "temp_f":    pd.array(_g("air_temp_set_1"), dtype="Float64").round().astype("Int64"),
        "dewp_f":    pd.array(_g("dew_point_temperature_set_1"), dtype="Float64").round().astype("Int64"),
        "wdir":      pd.array(_g("wind_direction_set_1"), dtype="Float64").round().astype("Int64"),
        "wspd_kt":   pd.array(_g("wind_speed_set_1"), dtype="Float64").round().astype("Int64"),
        "gust_wdir": pd.array([None] * len(times), dtype="object"),
        "gust_kt":   pd.array(_g("wind_gust_set_1"), dtype="Float64").round().astype("Int64"),
        "ptype":     [None] * len(times),
        "precip_in": [None] * len(times),
    })
    df["gust_wdir"] = df["gust_wdir"].astype("Int64")
    df = df.sort_values("timestamp").reset_index(drop=True)
    if data_dir is not None:
        df = coerce_to_reference(df, data_dir / f"asos_{station}.parquet")
    return df


def fetch_live_asos(station: str, *, source: str = "iem",
                    synoptic_token: str | None = None,
                    hours_back: int = 48,
                    data_dir: Path | None = None) -> pd.DataFrame:
    """Dispatch wrapper. `source ∈ {iem, synoptic}`. Falls back to IEM on Synoptic errors."""
    if source == "synoptic":
        if not synoptic_token:
            logger.warning("ASOS source=synoptic but no token; falling back to IEM")
            return fetch_live_asos_iem(station, hours_back=hours_back, data_dir=data_dir)
        try:
            df = fetch_live_asos_synoptic(station, synoptic_token,
                                          hours_back=hours_back, data_dir=data_dir)
            if df.empty:
                logger.warning("Synoptic ASOS returned empty; falling back to IEM")
                return fetch_live_asos_iem(station, hours_back=hours_back, data_dir=data_dir)
            return df
        except Exception:
            logger.exception("Synoptic ASOS failed; falling back to IEM")
            return fetch_live_asos_iem(station, hours_back=hours_back, data_dir=data_dir)
    return fetch_live_asos_iem(station, hours_back=hours_back, data_dir=data_dir)


# ---------------------------------------------------------------------------
# HRRR — anchor-aware Herbie fetcher (replaces 12Z-hardcoded backfill subprocess)
# ---------------------------------------------------------------------------

# KMDW grid cell in HRRR's Lambert Conformal grid (constants — grid doesn't change).
HRRR_GRID_IY = 665
HRRR_GRID_IX = 1169

# Combined GRIB regex pulling all 13 variables in a single subset per fxx.
# Mirrors backfill_hrrr.py's HRRR_SEARCH — DO NOT change without updating that file.
HRRR_SEARCH = (
    "(:TMP:2 m above ground"
    "|:DPT:2 m above ground"
    "|:UGRD:10 m above ground"
    "|:VGRD:10 m above ground"
    "|:GUST:surface"
    "|:MSLMA:mean sea level"
    "|:TCDC:entire atmosphere"
    "|:CAPE:surface"
    "|:LFTX:500-1000 mb"
    "|:DSWRF:surface"
    "|:SNOD:surface"
    "|:WEASD:surface"
    "|:HPBL:surface)"
)

_HRRR_VAR_MAP = {
    # cfgrib short_name -> our parquet column
    "t2m": "t2m", "d2m": "d2m", "u10": "u10", "v10": "v10",
    "gust": "gust", "mslma": "mslma", "tcc": "tcc",
    "cape": "cape", "lftx": "lftx",
    "sdswrf": "sdswrf", "sde": "sde", "sdwe": "sdwe", "blh": "blh",
}


def fxx_for_peak_window(init_dt: pd.Timestamp, station_tz: str = "America/Chicago",
                        peak_start_hour: int = 12, peak_end_hour: int = 16,
                        target_date=None) -> list[int]:
    init_dt = pd.Timestamp(init_dt)
    if init_dt.tz is None:
        init_dt = init_dt.tz_localize("UTC")
    if target_date is None:
        target_date_obj = init_dt.tz_convert(station_tz).date()
    else:
        target_date_obj = pd.Timestamp(target_date).date()
    date_str = target_date_obj.strftime("%Y-%m-%d")
    peak_start = pd.Timestamp(f"{date_str} {peak_start_hour:02d}:00:00").tz_localize(station_tz)
    peak_end   = pd.Timestamp(f"{date_str} {peak_end_hour:02d}:00:00").tz_localize(station_tz)
    fxx_start = int((peak_start.tz_convert("UTC") - init_dt).total_seconds() / 3600)
    fxx_end   = int((peak_end.tz_convert("UTC")   - init_dt).total_seconds() / 3600)
    return list(range(fxx_start, fxx_end + 1))


def latest_safe_hrrr_init(now_utc: pd.Timestamp | None = None,
                          publish_lag_min: int = 90,
                          synoptic_only: bool = True) -> pd.Timestamp:
    if now_utc is None:
        now_utc = pd.Timestamp.now(tz="UTC")
    elif now_utc.tz is None:
        now_utc = now_utc.tz_localize("UTC")
    threshold = now_utc - pd.Timedelta(minutes=publish_lag_min)
    candidate = threshold.floor("h")
    if synoptic_only:
        while candidate.hour not in (0, 6, 12, 18):
            candidate -= pd.Timedelta(hours=1)
    return candidate


def fetch_live_hrrr(anchor_dt_local: pd.Timestamp, *, station_tz: str = "America/Chicago",
                    publish_lag_min: int = 90, now_utc: pd.Timestamp | None = None,
                    data_dir: Path | None = None, station: str = "KMDW") -> pd.DataFrame:
    """Anchor-aware HRRR fetch. Picks the latest safely-published synoptic init, pulls
    the 13-var subset for the anchor's local peak window, returns a wide DataFrame
    matching `data/hrrr_12z_KMDW.parquet` schema.
    """
    try:
        from herbie import Herbie  # local import; not all envs have it
    except ImportError as e:
        raise RuntimeError("herbie-data not installed; pip install herbie-data") from e

    anchor = pd.Timestamp(anchor_dt_local)
    if anchor.tz is None:
        anchor = anchor.tz_localize(station_tz)
    if now_utc is None:
        now_utc = pd.Timestamp.now(tz="UTC")
    elif now_utc.tz is None:
        now_utc = now_utc.tz_localize("UTC")

    as_of_utc = min(anchor.tz_convert("UTC"), now_utc)
    init_dt = latest_safe_hrrr_init(as_of_utc, publish_lag_min=publish_lag_min)
    target_date = anchor.date()
    fxx_list = fxx_for_peak_window(init_dt, station_tz=station_tz, target_date=target_date)

    rows = []
    for fxx in fxx_list:
        H = Herbie(init_dt.strftime("%Y-%m-%d %H:%M"), model="hrrr", product="sfc",
                   fxx=fxx, verbose=False)
        try:
            ds_list = H.xarray(HRRR_SEARCH, remove_grib=True)
        except Exception as e:
            logger.warning("HRRR fxx=%d failed: %s: %s", fxx, type(e).__name__, e)
            continue
        if not isinstance(ds_list, list):
            ds_list = [ds_list]
        row = {
            "date":      pd.Timestamp(target_date),
            "init_dt":   init_dt,
            "fxx":       int(fxx),
            "valid_utc": init_dt + pd.Timedelta(hours=fxx),
        }
        for ds in ds_list:
            for var_short, col in _HRRR_VAR_MAP.items():
                if var_short in ds.data_vars:
                    row[col] = float(ds[var_short].isel(y=HRRR_GRID_IY, x=HRRR_GRID_IX).values)
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if data_dir is not None:
        df = coerce_to_reference(df, data_dir / f"hrrr_12z_{station}.parquet")
    return df


# ---------------------------------------------------------------------------
# Async parallel orchestrator — replaces refresh_data.py + backfill_hrrr.py subprocess pair
# ---------------------------------------------------------------------------

async def fetch_all_live(
    station: str,
    anchor_dt_local: pd.Timestamp,
    *,
    local_tz: str = "America/Chicago",
    asos_source: str = "iem",
    synoptic_token: str | None = None,
    data_dir: Path | None = None,
) -> dict[str, pd.DataFrame]:
    """Fetch all 5 weather sources concurrently. Returns dict keyed by source name.

    Wall time on a healthy network: ~3–5 s. Compare to ~30–90 s for the
    subprocess-serial pattern.
    """
    loop_anchor = anchor_dt_local

    async def _to_thread(label, fn, *args, **kwargs):
        try:
            return label, await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as e:
            logger.exception("live fetch [%s] failed: %s", label, e)
            return label, None

    coros = [
        _to_thread("metar", fetch_live_metar, station, limit=40, data_dir=data_dir),
        _to_thread("taf",   fetch_live_taf,   station, hours_back=36, data_dir=data_dir),
        _to_thread("asos",  fetch_live_asos,  station,
                   source=asos_source, synoptic_token=synoptic_token,
                   hours_back=48, data_dir=data_dir),
        _to_thread("cli",   fetch_live_cli,   station, days_back=30, data_dir=data_dir),
        _to_thread("hrrr",  fetch_live_hrrr,  loop_anchor,
                   station_tz=local_tz, data_dir=data_dir, station=station),
    ]
    pairs = await asyncio.gather(*coros)
    return {label: df for label, df in pairs}
