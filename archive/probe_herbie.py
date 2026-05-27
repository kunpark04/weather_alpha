"""Herbie probe: pull one historical HRRR run for KMDW and extract 2m temperature
for the noon–4 PM CDT peak window.

Goal: empirically validate the Herbie API surface (which methods exist, what
variable names cfgrib hands us, how the nearest-grid-cell logic shakes out)
BEFORE committing to a v3 architecture that depends on it. The script
deliberately prints intermediate structures (data_vars, coords, method list)
so we can confirm or correct the API claims we made from docs alone.

INSTALL (Windows + miniconda — the eccodes/cfgrib stack is finicky on pure pip):
    conda install -c conda-forge herbie-data
    # or, if that fails, the manual stack:
    # conda install -c conda-forge cfgrib eccodes xarray
    # pip install herbie-data

USE:
    python probe_herbie.py

WHAT IT DOES:
    1. Instantiates one Herbie object for HRRR 2024-07-15 06Z run, +12 h lead
       (valid 18Z = 1 PM CDT). This is the run that would have been operationally
       available for a T = 6 AM CDT prediction on 2024-07-15.
    2. Prints the Herbie object's attribute / method surface so we can validate
       which API claims hold.
    3. Pulls the 2m temperature subset via `H.xarray("TMP:2 m")` (verified syntax).
    4. Manually locates the nearest grid cell to KMDW (works regardless of HRRR's
       Lambert Conformal projection — does NOT depend on the unverified
       `nearest_points()` helper).
    5. Iterates through the noon–4 PM CDT peak window (fxx 11–15 from a 06Z init)
       and computes candidate aggregate features (max / mean / min / p90).
    6. Reports the HRRR peak-max forecast vs the known actual CLI high
       (91°F for 2024-07-15) so we have a sanity check on the whole pipeline
       end to end.

NEXT STEPS AFTER THIS RUNS CLEAN:
    - Wrap step 3+ into a function `get_hrrr_peak_features(date, init_hour_utc)`.
    - Backfill features over the whole training range using the Historical
      Forecast archive (HRRR available back to 2018-01-01).
    - Join the new columns into feature_df in §3.x of model_para_v2 and re-run
      §5 → §10.2 to measure CRPS lift.
"""

import numpy as np

# Defer the Herbie import so the script can still print the docstring banner
# if the package isn't installed — useful for first-run diagnostics.
try:
    from herbie import Herbie
except ImportError as e:
    raise SystemExit(
        "Herbie not installed. Try:\n"
        "    conda install -c conda-forge herbie-data\n"
        "Then re-run this script."
    ) from e


# ---- constants ----

KMDW_LAT = 41.7868
KMDW_LON = -87.7522

# 2024-07-15 was a known hot day; the existing predict_summary smoke test
# reports actual CLI high = 91°F. Good sanity-check ground truth.
TARGET_DATE     = "2024-07-15"
ACTUAL_CLI_HIGH = 91   # °F, from existing notebook smoke test

# For a T = 6 AM CDT (11Z) prediction, the safely-available HRRR run is 06Z
# (init at 1 AM CDT, published by ~2 AM CDT — comfortably before T).
INIT_RUN_UTC = f"{TARGET_DATE} 06:00"

# Peak heating window for KMDW: noon–4 PM CDT = 17–21 UTC. From a 06Z init:
#   fxx = 11 -> 17Z = 12 noon CDT
#   fxx = 15 -> 21Z =  4 PM CDT
LEAD_HOURS = list(range(11, 16))


# ---- Step 1: instantiate one Herbie object and inspect the API surface ----

print(f"=== Herbie HRRR probe ===")
print(f"  Init run:    {INIT_RUN_UTC}Z (HRRR)")
print(f"  Target date: {TARGET_DATE} (actual cli_high = {ACTUAL_CLI_HIGH}°F)")
print(f"  Lead hours:  {LEAD_HOURS}  (valid 17–21Z = noon–4 PM CDT)")
print()

print("--- Step 1: API inspection (one lead hour) ---")
H = Herbie(INIT_RUN_UTC, model="hrrr", product="sfc", fxx=12)

# Surface whatever attributes Herbie actually exposes — confirms or corrects
# the source-priority and find_grib claims we couldn't verify from docs.
public_attrs = [m for m in dir(H) if not m.startswith("_")]
print(f"  Public attributes on H ({len(public_attrs)} total, first 30 shown):")
for chunk in [public_attrs[i:i+6] for i in range(0, min(30, len(public_attrs)), 6)]:
    print(f"    {chunk}")
for attr_name in ("SOURCES", "grib", "grib_source", "idx_source"):
    if hasattr(H, attr_name):
        val = getattr(H, attr_name)
        print(f"  H.{attr_name} = {val!r}"[:140])


# ---- Step 2: pull 2m temperature subset and inspect the xarray Dataset ----

print("\n--- Step 2: pull TMP:2 m subset ---")
ds = H.xarray("TMP:2 m")
print(f"  Dataset overview:\n{ds}\n")
print(f"  data_vars: {list(ds.data_vars.keys())}")
print(f"  coords:    {list(ds.coords.keys())}")
print(f"  dims:      {dict(ds.sizes)}")


# ---- Step 3: nearest grid cell to KMDW (manual; projection-agnostic) ----

print("\n--- Step 3: locate nearest HRRR grid cell to KMDW ---")
temp_var = list(ds.data_vars.keys())[0]   # whatever cfgrib named the 2m-temp field
lat_arr = ds.latitude.values
lon_arr = ds.longitude.values
if (lon_arr > 180).any():
    # HRRR sometimes uses 0–360 longitude convention; normalize to -180..180
    lon_arr = np.where(lon_arr > 180, lon_arr - 360, lon_arr)

dist_sq = (lat_arr - KMDW_LAT) ** 2 + (lon_arr - KMDW_LON) ** 2
iy, ix = np.unravel_index(dist_sq.argmin(), dist_sq.shape)
print(f"  Variable used:       {temp_var}")
print(f"  Nearest grid cell:   ({lat_arr[iy, ix]:.4f}°N, {lon_arr[iy, ix]:.4f}°W)")
print(f"  Distance from KMDW:  ~{np.sqrt(dist_sq[iy, ix]) * 111:.2f} km")
print(f"  Index (iy, ix):      ({iy}, {ix})  [reuse for all lead hours below]")

t_k_check = float(ds[temp_var].isel(y=iy, x=ix).values)
t_f_check = (t_k_check - 273.15) * 9 / 5 + 32
print(f"  Sanity: fxx=12 (valid 18Z = 1 PM CDT) -> {t_k_check:.2f} K = {t_f_check:.2f}°F")


# ---- Step 4: pull all peak-window lead hours ----

print("\n--- Step 4: peak-window lead hours ---")
peak_temps_f = []
for fxx in LEAD_HOURS:
    H_fxx = Herbie(INIT_RUN_UTC, model="hrrr", product="sfc", fxx=fxx)
    ds_fxx = H_fxx.xarray("TMP:2 m")
    t_k = float(ds_fxx[temp_var].isel(y=iy, x=ix).values)
    t_f = (t_k - 273.15) * 9 / 5 + 32
    valid_utc = (6 + fxx) % 24
    valid_cdt = (valid_utc - 5) % 24
    print(f"  fxx={fxx:>2}  valid {valid_utc:02d}Z = {valid_cdt:>2}:00 CDT:  T_2m = {t_f:.2f}°F")
    peak_temps_f.append(t_f)


# ---- Step 5: candidate aggregate features ----

print("\n--- Step 5: candidate HRRR peak-window features ---")
hrrr_temp_max_peak  = float(max(peak_temps_f))
hrrr_temp_mean_peak = float(np.mean(peak_temps_f))
hrrr_temp_min_peak  = float(min(peak_temps_f))
hrrr_temp_p90_peak  = float(np.percentile(peak_temps_f, 90))

print(f"  hrrr_temp_max_peak  = {hrrr_temp_max_peak:.2f}°F  <- strongest single feature for cli_high")
print(f"  hrrr_temp_mean_peak = {hrrr_temp_mean_peak:.2f}°F")
print(f"  hrrr_temp_min_peak  = {hrrr_temp_min_peak:.2f}°F")
print(f"  hrrr_temp_p90_peak  = {hrrr_temp_p90_peak:.2f}°F")

print()
print(f"  Actual cli_high on {TARGET_DATE}: {ACTUAL_CLI_HIGH}°F")
print(f"  HRRR peak-max forecast:  {hrrr_temp_max_peak:.2f}°F")
print(f"  |delta|:                 {abs(ACTUAL_CLI_HIGH - hrrr_temp_max_peak):.2f}°F")
print()
print("If |delta| is < ~3°F, the pipeline is producing physically sensible HRRR")
print("forecasts at KMDW. If |delta| > ~5°F, double-check the grid-cell lookup and")
print("Kelvin->Fahrenheit conversion before trusting this as a feature.")
