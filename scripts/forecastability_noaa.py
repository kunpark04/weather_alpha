"""Kalshi-INDEPENDENT, ~10-year forecastability RCA: does a city's wing COVERAGE track its
1 PM-CONDITIONAL residual high uncertainty, measured from EXACT-degF NOAA hourly obs?

WHY THIS SCRIPT EXISTS (read CLAUDE.md + the two priors it sits between):
  * `scripts/forecast_error_rca.py` found, on a 67-day Kalshi-modal forecast error, that low-coverage
    cities have a WIDER 1 PM-conditional error spread (Spearman -0.75) and more big misses
    (tails -0.69) but NOT a bias (|bias| -0.19). Limits: 67 days, +-1F bucket resolution, and the
    "forecast" was the Kalshi market modal -> NOT Kalshi-independent.
  * `scripts/predictability_check.py` used UNCONDITIONAL daily-high volatility (std of the settled
    high, std of its day-over-day change) and got a NULL (coverage uncorrelated). That is the WRONG
    measure: it is not conditional on what is already known at 1 PM.

This script confirms/refutes the -0.75 finding over ~10 years of hourly NOAA ASOS (exact degF),
reconstructing the 1 PM-CONDITIONAL residual from HOURLY data (NOT daily highs):

  For each station, each LOCAL calendar day:
    T_1pm    = temperature at the last hourly ob at or before 13:00 local
    Tmax     = max temperature over the local calendar day
    aft_rise = Tmax - T_1pm   (the still-unrealized part of the day's high, AT 1 PM)
  Per-station forecastability proxies over the full history:
    spread_cond  = std(aft_rise)                         <- 1 PM-conditional analog of the -0.75 driver (PRIMARY)
    tail_cond    = P(aft_rise - median(aft_rise) >= 4F)  <- big late surprises
    resid_cond   = std(aft_rise - month-of-year climatology mean)  <- de-seasonalized conditional spread
  UNCONDITIONAL contrasts (to re-test the earlier null at exact-degF over years):
    spread_uncond = std(Tmax)
    dod_uncond    = std(day-over-day change in Tmax)

Coverage is pulled FROM THE REPO (apples-to-apples with the live wing): `rca_city` over each
city's Kalshi backfill (market_wing, drop_lower_ask, gate<=0.90). Cities with fired<8 are dropped
(LA fires ~1 day -> meaningless coverage; it distorted the Kalshi corr from -0.50 to -0.75 once
removed). Cross-city unit = city.

Data: Iowa Environmental Mesonet ASOS service (no auth, hourly, degF), cached under
data/noaa_actuals/<STID>.csv so re-runs don't refetch. Timestamps are UTC -> converted to each
station's LOCAL tz (from the repo CITY tz) before the local-day reduction. 'M'/'T'/blank handled.

CAVEATS (honest):
  * aft_rise spread is a FORECASTABILITY PROXY, not a true forecast error (no model is run); it
    measures how much of the high is still unrealized + how variable that is at 1 PM.
  * The ASOS station is the best match to Kalshi's settlement sensor but may not be byte-identical
    to Kalshi's CLI source on every day. Stations READ from repo config vs INFERRED are flagged below.
  * Hourly ASOS misses sub-hourly peaks vs 1-min ASOS -> Tmax slightly understated, uniformly across
    cities, so cross-city ranking is largely unaffected.

Usage:
  python scripts/forecastability_noaa.py                  # all cities, ~2015-01-01..today
  python scripts/forecastability_noaa.py --start 2010-01-01
  python scripts/forecastability_noaa.py --no-fetch       # use only cached CSVs (offline)
"""
from __future__ import annotations

import argparse
import sys
import time
from io import StringIO
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from scripts.rca_profitable_vs_losing import rca_city
from weather_alpha.config import load_config

NOAA_DIR = _ROOT / "data" / "noaa_actuals"
IEM_URL = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

# ---------------------------------------------------------------------------
# Station mapping: Kalshi series -> (Mesonet ASOS station id WITHOUT leading K, source-of-truth).
# READ  = present in repo config (config/{live,paper}.yaml markets[].station).
# INFER = official NWS/ASOS settlement sensor for that Kalshi market, inferred (NOT in repo config).
# The Mesonet id is the 3-4 letter ASOS id; US ids are the ICAO minus the leading 'K'
# (KMDW -> MDW). NYC=Central Park (NYC), DC=Reagan National (DCA), Austin=Camp Mabry (also
# logged as 'AUS' arpt on Mesonet; Kalshi's KAUS market settles on the airport ASOS -> AUS).
# ---------------------------------------------------------------------------
STATION = {
    "KXHIGHCHI":   ("MDW", "READ"),    # Chicago Midway      -- config/live.yaml & paper.yaml
    "KXHIGHTHOU":  ("HOU", "READ"),    # Houston Hobby       -- config/paper.yaml
    "KXHIGHNY":    ("NYC", "INFER"),   # New York Central Park
    "KXHIGHMIA":   ("MIA", "INFER"),   # Miami Intl
    "KXHIGHAUS":   ("AUS", "INFER"),   # Austin-Bergstrom (airport ASOS)
    "KXHIGHDEN":   ("DEN", "INFER"),   # Denver Intl
    "KXHIGHPHIL":  ("PHL", "INFER"),   # Philadelphia Intl
    "KXHIGHLAX":   ("LAX", "INFER"),   # Los Angeles Intl  (city dropped: fires <8)
    "KXHIGHTLV":   ("LAS", "INFER"),   # Las Vegas Harry Reid
    "KXHIGHTNOLA": ("MSY", "INFER"),   # New Orleans Louis Armstrong
    "KXHIGHTSEA":  ("SEA", "INFER"),   # Seattle-Tacoma
    "KXHIGHTSFO":  ("SFO", "INFER"),   # San Francisco Intl
    "KXHIGHTDC":   ("DCA", "INFER"),   # Washington Reagan National
    "KXHIGHTATL":  ("ATL", "INFER"),   # Atlanta Hartsfield
    "KXHIGHTMIN":  ("MSP", "INFER"),   # Minneapolis-St Paul
    "KXHIGHTPHX":  ("PHX", "INFER"),   # Phoenix Sky Harbor
    "KXHIGHTBOS":  ("BOS", "INFER"),   # Boston Logan
    "KXHIGHTDAL":  ("DAL", "INFER"),   # Dallas Love Field
    "KXHIGHTOKC":  ("OKC", "INFER"),   # Oklahoma City Will Rogers
    "KXHIGHTSATX": ("SAT", "INFER"),   # San Antonio Intl
}


# ---------------------------------------------------------------------------
# Fetch + cache hourly ASOS tmpf (UTC), polite: one request per station, year-chunk fallback.
# ---------------------------------------------------------------------------
def _iem_params(stid: str, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    return {
        "station": stid, "data": "tmpf",
        "year1": start.year, "month1": start.month, "day1": start.day,
        "year2": end.year, "month2": end.month, "day2": end.day,
        "tz": "Etc/UTC", "format": "onlycomma", "missing": "M", "trace": "T",
    }


def _get_csv(stid: str, start: pd.Timestamp, end: pd.Timestamp, timeout: int = 180) -> pd.DataFrame:
    r = requests.get(IEM_URL, params=_iem_params(stid, start, end), timeout=timeout)
    r.raise_for_status()
    txt = r.text
    if txt.lstrip().lower().startswith("too many requests"):
        raise RuntimeError("rate-limited")
    df = pd.read_csv(StringIO(txt))
    return df


def fetch_station(stid: str, start: pd.Timestamp, end: pd.Timestamp,
                  *, allow_fetch: bool, polite_s: float = 4.0) -> pd.DataFrame:
    """Return raw hourly rows [station, valid(UTC str), tmpf] for `stid`, cached at
    data/noaa_actuals/<stid>.csv. Single full-range request; on failure, chunk by year.
    If the cache exists it is reused as-is (assumed to cover the window; re-fetch by deleting)."""
    NOAA_DIR.mkdir(parents=True, exist_ok=True)
    cache = NOAA_DIR / f"{stid}.csv"
    if cache.exists() and cache.stat().st_size > 200:
        return pd.read_csv(cache)
    if not allow_fetch:
        return pd.DataFrame()

    df = pd.DataFrame()
    try:
        df = _get_csv(stid, start, end)
    except Exception as e:
        print(f"    [{stid}] full-range fetch failed ({type(e).__name__}: {e}); chunking by year",
              flush=True)
        frames = []
        for yr in range(start.year, end.year + 1):
            s = max(start, pd.Timestamp(yr, 1, 1))
            e = min(end, pd.Timestamp(yr + 1, 1, 1))
            for attempt in range(4):
                try:
                    frames.append(_get_csv(stid, s, e))
                    break
                except Exception as ex:
                    time.sleep(2.0 * (attempt + 1))
                    if attempt == 3:
                        print(f"    [{stid}] year {yr} failed: {ex}", flush=True)
            time.sleep(polite_s)
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    if df.empty or "valid" not in df.columns:
        return pd.DataFrame()
    df = df[["station", "valid", "tmpf"]].copy()
    df.to_csv(cache, index=False)
    time.sleep(polite_s)   # be polite between stations
    return df


# ---------------------------------------------------------------------------
# Per-station 1 PM-conditional reduction from hourly obs.
# ---------------------------------------------------------------------------
def daily_reduce(raw: pd.DataFrame, tz: ZoneInfo) -> pd.DataFrame:
    """raw[station,valid(UTC),tmpf] -> per local-day [date, T_1pm, Tmax, aft_rise, n_obs].
    A day is kept only if it has an ob at/<=13:00 local AND coverage spanning the afternoon
    (>= ~12 hourly obs and at least one ob after 13:00, so Tmax isn't truncated at 1 PM)."""
    t = pd.to_numeric(raw["tmpf"], errors="coerce")       # 'M'/'T'/blank -> NaN
    ts = pd.to_datetime(raw["valid"], utc=True, errors="coerce")
    s = pd.DataFrame({"ts": ts, "t": t}).dropna()
    if s.empty:
        return pd.DataFrame()
    loc = s["ts"].dt.tz_convert(tz)
    s = s.assign(day=loc.dt.normalize().dt.tz_localize(None),
                 hr=loc.dt.hour + loc.dt.minute / 60.0,
                 month=loc.dt.month)
    rows = []
    for day, g in s.groupby("day"):
        pre = g[g["hr"] <= 13.0]
        post = g[g["hr"] > 13.0]
        if pre.empty or post.empty or len(g) < 12:
            continue
        t_1pm = float(pre.sort_values("hr")["t"].iloc[-1])  # last ob at/<=13:00
        tmax = float(g["t"].max())
        rows.append({"date": day, "month": int(g["month"].iloc[0]),
                     "T_1pm": t_1pm, "Tmax": tmax,
                     "aft_rise": tmax - t_1pm, "n_obs": int(len(g))})
    return pd.DataFrame(rows)


def proxies(d: pd.DataFrame) -> dict:
    """Forecastability proxies from the per-day reduction."""
    ar = d["aft_rise"].to_numpy(float)
    tmax = d["Tmax"].to_numpy(float)
    med = float(np.median(ar))
    # de-seasonalized conditional residual: subtract month-of-year mean aft_rise
    clim = d.groupby("month")["aft_rise"].transform("mean")
    resid = (d["aft_rise"] - clim).to_numpy(float)
    # unconditional day-over-day change in Tmax (chronological)
    dser = d.sort_values("date")
    dod = dser["Tmax"].diff().dropna().to_numpy(float)
    return {
        "n_days": int(len(d)),
        "spread_cond": float(np.std(ar)),
        "tail_cond": float(np.mean((ar - med) >= 4.0)),
        "resid_cond": float(np.std(resid)),
        "mean_aft_rise": float(np.mean(ar)),
        "spread_uncond": float(np.std(tmax)),
        "dod_uncond": float(np.std(dod)) if dod.size else float("nan"),
    }


# ---------------------------------------------------------------------------
# Correlation helper (mirror forecast_error_rca._corr: Pearson + Spearman, finite-masked).
# ---------------------------------------------------------------------------
def _corr(a, b):
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 3:
        return float("nan"), float("nan")
    pe = float(np.corrcoef(a[m], b[m])[0, 1])
    sp = float(np.corrcoef(pd.Series(a[m]).rank(), pd.Series(b[m]).rank())[0, 1])
    return pe, sp


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default=None, help="default = today")
    ap.add_argument("--no-fetch", action="store_true", help="use only cached CSVs")
    args = ap.parse_args(argv)

    start = pd.Timestamp(args.start)
    end = pd.Timestamp(args.end) if args.end else pd.Timestamp.now().normalize() + pd.Timedelta(days=1)
    allow_fetch = not args.no_fetch

    cfg = load_config()
    print(f"NOAA forecastability RCA | window {start.date()}..{end.date()} | "
          f"fetch={'on' if allow_fetch else 'OFF (cache only)'} | cache={NOAA_DIR}")

    rows = []
    miss_frac = {}
    for s in CITY:
        if s not in STATION or not (BACKFILL / s).is_dir():
            continue
        # --- coverage from the repo (apples-to-apples) ---
        bf = load_city(s)
        if bf.empty:
            continue
        r = rca_city(bf, s, cfg)
        if not r or r["fired"] < 8:        # drop LA-style sub-8-fire cities (meaningless coverage)
            continue

        stid, src = STATION[s]
        tz = ZoneInfo(CITY[s][1])
        print(f"  {CITY[s][0]:<14} {stid} ({src})", flush=True)
        raw = fetch_station(stid, start, end, allow_fetch=allow_fetch)
        if raw.empty:
            print(f"    [{stid}] no hourly data (cache miss + fetch off/failed); skipped", flush=True)
            continue
        # missing-data fraction = blank/M/T over rows in window
        tnum = pd.to_numeric(raw.get("tmpf"), errors="coerce")
        miss_frac[s] = float(tnum.isna().mean()) if len(tnum) else float("nan")

        d = daily_reduce(raw, tz)
        if len(d) < 200:                   # need a real multi-year sample per station
            print(f"    [{stid}] only {len(d)} usable local-days; skipped", flush=True)
            continue
        p = proxies(d)
        rows.append({"city": CITY[s][0], "stid": stid, "src": src,
                     "coverage": r["coverage"], "fired": r["fired"],
                     "miss_frac": miss_frac.get(s, float("nan")), **p})

    if not rows:
        print("\nNo cities produced. If offline, run once with network to populate "
              f"{NOAA_DIR}, then re-run with --no-fetch.")
        return 1

    d = pd.DataFrame(rows).sort_values("coverage", ascending=False).reset_index(drop=True)

    # ---- per-city table ----
    print("\n" + "=" * 104)
    print("PER-CITY 1 PM-CONDITIONAL FORECASTABILITY  (exact-degF NOAA hourly; sorted by coverage)")
    print("=" * 104)
    hdr = (f"{'city':<14}{'stid':>5}{'src':>6}{'fired':>6}{'cover':>7}{'n_days':>7}"
           f"{'sprd_cond':>10}{'tail_cond':>10}{'resid_cnd':>10}{'sprd_unc':>9}{'dod_unc':>8}{'miss%':>7}")
    print(hdr)
    for _, x in d.iterrows():
        print(f"{x['city']:<14}{x['stid']:>5}{x['src']:>6}{int(x['fired']):>6}{x['coverage']:>7.0%}"
              f"{int(x['n_days']):>7}{x['spread_cond']:>10.2f}{x['tail_cond']:>10.0%}"
              f"{x['resid_cond']:>10.2f}{x['spread_uncond']:>9.2f}{x['dod_uncond']:>8.2f}"
              f"{x['miss_frac']:>7.1%}")

    # ---- cross-city correlations vs coverage ----
    print("\nCROSS-CITY: coverage vs each proxy  (negative = that uncertainty lowers coverage)")
    print(f"  {'proxy':<34}{'Spearman':>10}{'Pearson':>10}    Kalshi-67d ref")
    refs = {"spread_cond": "spread -0.75", "tail_cond": "tails  -0.69",
            "resid_cond": "(de-seasoned analog of -0.75)", "spread_uncond": "(uncond; prior NULL)",
            "dod_uncond": "(uncond dod; prior NULL)"}
    for k, lab in [("spread_cond", "spread_cond  (1PM-cond forecastability)"),
                   ("tail_cond", "tail_cond    (big late surprises >=4F)"),
                   ("resid_cond", "resid_cond   (de-seasonalized cond)"),
                   ("spread_uncond", "spread_uncond(std of Tmax)"),
                   ("dod_uncond", "dod_uncond   (std day-over-day Tmax)")]:
        pe, sp = _corr(d["coverage"], d[k])
        print(f"  {lab:<34}{sp:>+10.2f}{pe:>+10.2f}    {refs[k]}")

    # ---- hi-cov vs lo-cov group means ----
    win = d[d["coverage"] >= 0.85]; los = d[d["coverage"] < 0.85]
    print(f"\ngroup means        hi-cov (>=85%, n={len(win)})   vs   lo-cov (<85%, n={len(los)})")
    for k in ["spread_cond", "tail_cond", "resid_cond", "spread_uncond", "dod_uncond"]:
        wv, lv = win[k].mean(), los[k].mean()
        print(f"  {k:<14} {wv:>9.3f}   {lv:>9.3f}   (lo - hi = {lv - wv:+.3f})")

    # ---- explicit Kalshi-67d comparison ----
    print("\nCompare to the Kalshi 67-day, +-1F bucket RCA (forecast_error_rca.py):")
    print("  spread  Spearman -0.75   |   tails  Spearman -0.69   |   |bias|  Spearman -0.19")
    print("  This script's PRIMARY analog is spread_cond (1 PM-conditional, exact-degF, ~10y).")

    # ---- station-source + missing-data footnote ----
    inferred = [f"{x['city']}={x['stid']}" for _, x in d.iterrows() if x["src"] == "INFER"]
    read = [f"{x['city']}={x['stid']}" for _, x in d.iterrows() if x["src"] == "READ"]
    print(f"\nstations READ from repo config: {', '.join(read) if read else '(none)'}")
    print(f"stations INFERRED (not in config): {', '.join(inferred)}")
    print(f"worst missing-tmpf fraction: "
          f"{d.loc[d['miss_frac'].idxmax(), 'city']} {d['miss_frac'].max():.1%}"
          if d["miss_frac"].notna().any() else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
