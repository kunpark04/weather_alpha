#!/usr/bin/env python
"""backfill_hrrr.py -- backfill 12Z HRRR forecast variables for KMDW peak window.

Targeted at the v3 model (T = 1 PM local clock anchor). The 12Z run publishes
by ~13:30 UTC (8:30 AM CDT / 7:30 AM CST), comfortably before the 1 PM anchor.
fxx range auto-derived per date to handle DST transitions correctly.

PER-DAY WORK:
  - One Herbie call per fxx, with a combined regex search pulling 9 variables
    (t2m, d2m, u10, v10, gust, prmsl, tcc, cape, lftx) in a single GRIB subset.
  - ~5 fxx values per day (summer: 5-9; winter: 6-10).
  - Disk hygiene: delete cached GRIB subset files after extraction.

CONCURRENCY:
  - 16 threads via ThreadPoolExecutor (one per day, network-bound).
  - HRRR archive on AWS S3 (noaa-hrrr-bdp-pds) handles concurrent reads cleanly.

RESUMABILITY:
  - Reads existing parquet; only fetches dates not already covered.
  - Checkpoints every N days (default 100) so a crash loses at most that many
    days of work.

OUTPUT:
  data/hrrr_12z_KMDW.parquet -- long format:
    [date, init_dt, fxx, valid_utc, t2m, d2m, u10, v10, gust, prmsl, tcc, cape, lftx]

USAGE:
  python backfill_hrrr.py
  python backfill_hrrr.py --start 2024-07-15 --end 2024-07-20  (small smoke)
  python backfill_hrrr.py --threads 8 --checkpoint-every 50
"""

import argparse
import tempfile
import time
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

import numpy as np
import pandas as pd
import xarray as xr
from herbie import Herbie


# ============================================================================
# Constants -- single source of truth shared with live.ipynb's fetcher.
# ============================================================================

STATION       = "KMDW"
LOCAL_TZ      = "America/Chicago"
INIT_HOUR_UTC = 12              # 12Z is the safe init for T = 1 PM local anchor

# KMDW grid cell in HRRR's Lambert Conformal projection. Constants -- the grid
# doesn't change between runs. Verified in herbie.ipynb §3.
HRRR_GRID_IY = 665
HRRR_GRID_IX = 1169

# Peak heating window: noon to 4 PM local clock (matches model_v3 PEAK_*_HOUR_LOCAL).
PEAK_START_HOUR = 12
PEAK_END_HOUR   = 16

# Combined GRIB regex: pulls 9 variables in a single subset per fxx.
# HRRR sfc uses MSLMA ("MAPS sea level pressure") -- NOT PRMSL. Inventory check:
#   `:MSLMA:mean sea level:N hour fcst:` is present;
#   `:PRMSL:mean sea level:...` is NOT in HRRR sfc. Using PRMSL matches zero
#   records, causing Herbie to fail with a FileNotFoundError on the empty
#   subset file. cfgrib's short_name for MSLMA is "mslma".
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
    # Path-5 additions: radiation, snow, mixing depth, soil temp
    "|:DSWRF:surface"
    "|:SNOD:surface"
    "|:WEASD:surface"
    "|:HPBL:surface)"
)
EXPECTED_VARS = [
    # Original 9 vars (verified)
    "t2m", "d2m", "u10", "v10", "gust", "mslma", "tcc", "cape", "lftx",
    # Path-5 additions (cfgrib short_names -- smoke-tested before full backfill)
    "sdswrf", # downward shortwave radiation flux at surface (W/m^2)
    "sde",    # snow depth (m)
    "sdwe",   # snow depth water equivalent (mm)
    "blh",    # planetary boundary layer height (m)
]

OUTPUT_PATH = Path("data/hrrr_12z_KMDW.parquet")
OUTPUT_PATH.parent.mkdir(exist_ok=True)


# ============================================================================
# Lead-hour computation -- DST-safe (matches live.ipynb's fxx_for_peak_window).
# ============================================================================

def fxx_for_peak_window(init_dt, station_tz=LOCAL_TZ,
                       peak_start_hour=PEAK_START_HOUR, peak_end_hour=PEAK_END_HOUR,
                       target_date=None):
    """Compute the fxx list to cover the local peak window for target_date.

    DST-safe: builds naive local timestamps for peak_start/end_hour on
    target_date and then localizes. Avoids the "midnight-local + 12h UTC"
    pitfall that shifts the window 1 hour on DST transition days.

    Args:
    - init_dt (str | pd.Timestamp): HRRR init time.
    - target_date (datetime.date | pd.Timestamp | None): local date to target.
      If None, uses init_dt's local date.

    Returns:
    - list[int]: lead hours covering the peak window on target_date.
    """
    init_dt = pd.Timestamp(init_dt)
    if init_dt.tz is None:
        init_dt = init_dt.tz_localize("UTC")

    if target_date is None:
        target_date_obj = init_dt.tz_convert(station_tz).date()
    else:
        target_date_obj = pd.Timestamp(target_date).date()

    date_str = target_date_obj.strftime("%Y-%m-%d")
    peak_start_local = pd.Timestamp(f"{date_str} {peak_start_hour:02d}:00:00").tz_localize(station_tz)
    peak_end_local   = pd.Timestamp(f"{date_str} {peak_end_hour:02d}:00:00").tz_localize(station_tz)

    fxx_start = int((peak_start_local.tz_convert("UTC") - init_dt).total_seconds() / 3600)
    fxx_end   = int((peak_end_local.tz_convert("UTC")   - init_dt).total_seconds() / 3600)
    return list(range(fxx_start, fxx_end + 1))


# ============================================================================
# Per-day worker -- fetches all peak-window fxx for one date's 12Z run.
# ============================================================================

def fetch_one_day(date_str):
    """Fetch peak-window fxx values for one date's 12Z run.

    THREAD-SAFE: each fxx fetch uses its own `tempfile.TemporaryDirectory` as
    Herbie's save_dir, isolating the cache state across concurrent threads.
    Herbie's default global cache directory (~/data/hrrr/YYYYMMDD/) is NOT
    thread-safe -- multiple threads writing to it cause silent file races
    where some fxx fetches return empty/missing data.

    Returns:
    - list[dict]: one row per fxx, with date + init_dt + valid_utc + 9 variable values.
      Empty list if all fxx fetches failed.
    """
    init_dt = pd.Timestamp(date_str).tz_localize("UTC") + pd.Timedelta(hours=INIT_HOUR_UTC)
    fxx_list = fxx_for_peak_window(init_dt)

    rows = []

    for fxx in fxx_list:
        # Per-fxx temp dir = automatic cleanup + zero cache race risk across threads
        with tempfile.TemporaryDirectory(prefix=f"herbie_{date_str}_f{fxx}_") as tmp_dir:
            try:
                H = Herbie(init_dt.strftime("%Y-%m-%d %H:%M"),
                           model="hrrr", product="sfc", fxx=fxx,
                           save_dir=Path(tmp_dir), verbose=False)
                result = H.xarray(HRRR_SEARCH)
            except Exception as e:
                print(f"  [{date_str} fxx={fxx}] FAILED: {type(e).__name__}: {e}", flush=True)
                continue

            datasets = [result] if isinstance(result, xr.Dataset) else list(result)

            valid_utc = init_dt + pd.Timedelta(hours=fxx)
            row = {
                "date":      pd.Timestamp(date_str).normalize(),
                "init_dt":   init_dt,
                "fxx":       int(fxx),
                "valid_utc": valid_utc,
            }
            for v in EXPECTED_VARS:
                row[v] = None

            for ds_one in datasets:
                for vname, da in ds_one.data_vars.items():
                    if vname in EXPECTED_VARS:
                        try:
                            row[vname] = float(da.isel(y=HRRR_GRID_IY, x=HRRR_GRID_IX).values)
                        except Exception:
                            pass
            rows.append(row)
        # tmp_dir auto-deleted by context manager -- no manual cleanup needed

    return rows


# ============================================================================
# Main: resumable, threaded backfill.
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Backfill 12Z HRRR for KMDW peak window")
    parser.add_argument("--start", default="2020-01-01",
                       help="Inclusive start date (YYYY-MM-DD)")
    parser.add_argument("--end",   default=pd.Timestamp.now().strftime("%Y-%m-%d"),
                       help="Inclusive end date (YYYY-MM-DD)")
    parser.add_argument("--threads", type=int, default=16,
                       help="Concurrent worker processes (default 16). The arg "
                            "name is 'threads' for shell-history convenience; the "
                            "actual implementation uses ProcessPoolExecutor because "
                            "eccodes is not thread-safe.")
    parser.add_argument("--checkpoint-every", type=int, default=100,
                       help="Flush parquet every N days completed (default 100)")
    parser.add_argument("--output", default=None,
                       help="Output parquet path (default: data/hrrr_12z_{STATION}.parquet)")
    args = parser.parse_args()

    # Override OUTPUT_PATH from CLI if --output was given (lets smoke tests write
    # to a different file without touching the production parquet).
    global OUTPUT_PATH
    if args.output is not None:
        OUTPUT_PATH = Path(args.output)
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    start_date = pd.Timestamp(args.start)
    end_date   = pd.Timestamp(args.end)
    all_dates  = pd.date_range(start_date, end_date, freq="D")

    # Resumability
    if OUTPUT_PATH.exists():
        existing = pd.read_parquet(OUTPUT_PATH)
        done_dates = set(pd.to_datetime(existing["date"]).dt.normalize().unique())
        todo = [d for d in all_dates if d not in done_dates]
        print(f"Resuming: {len(done_dates)} dates already in {OUTPUT_PATH.name}, "
              f"{len(todo)} to fetch.")
    else:
        existing = pd.DataFrame()
        todo = list(all_dates)
        print(f"Fresh build: {len(todo)} dates to fetch.")

    if not todo:
        print("Nothing to do.")
        return

    print(f"Workers:     {args.threads}  (processes, not threads -- eccodes is not thread-safe)")
    print(f"Date range:  {start_date.date()} -> {end_date.date()}")
    print(f"Per day:     ~5 fxx with combined regex (13 variables / fetch)")
    print(f"Output:      {OUTPUT_PATH}")
    print()

    accumulator = []
    t0 = time.time()
    done_count = 0

    # NOTE: ProcessPoolExecutor (not Thread) -- eccodes (the GRIB library cfgrib
    # uses) maintains global non-thread-safe state. Each process gets its own
    # eccodes instance, avoiding race conditions and "Decoding invalid" errors.
    # Process startup overhead is ~1-2 s per worker on Windows; negligible
    # vs the ~20 min total runtime for the full backfill.
    with ProcessPoolExecutor(max_workers=args.threads) as executor:
        future_to_date = {
            executor.submit(fetch_one_day, d.strftime("%Y-%m-%d")): d
            for d in todo
        }
        for future in as_completed(future_to_date):
            d = future_to_date[future]
            try:
                rows = future.result()
                accumulator.extend(rows)
            except Exception as e:
                print(f"[{d.date()}] FAILED: {type(e).__name__}: {e}", flush=True)

            done_count += 1
            if done_count % args.checkpoint_every == 0 or done_count == len(todo):
                elapsed = time.time() - t0
                rate    = done_count / elapsed
                eta_min = (len(todo) - done_count) / rate / 60 if rate > 0 else float("inf")
                print(f"  [{done_count:>5}/{len(todo)}] {d.date()}  "
                      f"elapsed={elapsed/60:5.1f}min  rate={rate:.2f} day/s  "
                      f"eta={eta_min:5.1f}min", flush=True)

                # Checkpoint flush
                new_df = pd.DataFrame(accumulator)
                if len(existing) > 0:
                    flushed = pd.concat([existing, new_df], ignore_index=True)
                else:
                    flushed = new_df
                if len(flushed) > 0:
                    flushed.to_parquet(OUTPUT_PATH)

    # Final flush
    new_df = pd.DataFrame(accumulator)
    if len(existing) > 0:
        final = pd.concat([existing, new_df], ignore_index=True)
    else:
        final = new_df
    final = final.sort_values(["date", "fxx"]).reset_index(drop=True)
    final.to_parquet(OUTPUT_PATH)

    total_min = (time.time() - t0) / 60
    print(f"\nDone. Total wall time: {total_min:.1f} min")
    print(f"Wrote {len(final)} rows to {OUTPUT_PATH}")
    print(f"Distinct dates: {final['date'].nunique()}")
    rows_per_day = len(final) / max(1, final['date'].nunique())
    print(f"Avg rows per day: {rows_per_day:.2f}  (target ~5)")


if __name__ == "__main__":
    main()
