"""Data layer — refresh IEM + HRRR parquets, load them, pick an anchor.

Wraps the existing `refresh_data.py` and `backfill_hrrr.py` scripts as
subprocesses (inherits the calling Python so metar/herbie env stays consistent).
Mirrors live_predict.ipynb §2.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataBundle:
    metar: pd.DataFrame
    taf: pd.DataFrame
    asos: pd.DataFrame
    cli: pd.DataFrame
    hrrr: pd.DataFrame


def refresh_iem(project_root: Path, timeout: int = 600) -> None:
    """Run refresh_data.py to update METAR/TAF/ASOS/CLI parquets."""
    script = project_root / "refresh_data.py"
    if not script.exists():
        raise FileNotFoundError(f"refresh_data.py not found at {script}")

    logger.info("Refreshing IEM parquets via %s ...", script.name)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True, text=True, cwd=project_root, timeout=timeout,
    )
    if proc.returncode != 0:
        logger.error("refresh_data.py failed (rc=%d):\n%s", proc.returncode, proc.stderr[-2000:])
        raise RuntimeError("refresh_data.py failed")
    logger.info("IEM refresh complete in %.1fs", time.perf_counter() - t0)


def refresh_hrrr(
    project_root: Path,
    local_tz: str,
    lookback_days: int = 4,
    threads: int = 4,
    timeout: int = 1800,
) -> None:
    """Run backfill_hrrr.py to fill any missing recent HRRR runs."""
    script = project_root / "backfill_hrrr.py"
    if not script.exists():
        raise FileNotFoundError(f"backfill_hrrr.py not found at {script}")

    hstart = (pd.Timestamp.now(tz=local_tz).normalize() - pd.Timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    hend = pd.Timestamp.now(tz=local_tz).normalize().strftime("%Y-%m-%d")

    logger.info("Extending HRRR coverage %s -> %s ...", hstart, hend)
    t0 = time.perf_counter()
    proc = subprocess.run(
        [sys.executable, str(script), "--start", hstart, "--end", hend, "--threads", str(threads)],
        capture_output=True, text=True, cwd=project_root, timeout=timeout,
    )
    if proc.returncode != 0:
        logger.warning(
            "backfill_hrrr.py returned rc=%d (HRRR may be stale):\n%s",
            proc.returncode, proc.stderr[-1500:],
        )
    logger.info("HRRR refresh complete in %.1fs", time.perf_counter() - t0)


def load_bundle(data_dir: Path, station: str) -> DataBundle:
    """Load all five parquets into a DataBundle. Fail loudly on missing files."""
    paths = {
        "metar": data_dir / f"metar_{station}.parquet",
        "taf":   data_dir / f"taf_{station}.parquet",
        "asos":  data_dir / f"asos_{station}.parquet",
        "cli":   data_dir / f"cli_{station}.parquet",
        "hrrr":  data_dir / f"hrrr_12z_{station}.parquet",
    }
    missing = [n for n, p in paths.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing parquets: {missing} (run refresh first)")
    return DataBundle(
        metar=pd.read_parquet(paths["metar"]),
        taf=pd.read_parquet(paths["taf"]),
        asos=pd.read_parquet(paths["asos"]),
        cli=pd.read_parquet(paths["cli"]),
        hrrr=pd.read_parquet(paths["hrrr"]),
    )


def latest_viable_anchor(
    bundle: DataBundle,
    t_hour_local: int,
    local_tz: str,
    now_local: pd.Timestamp | None = None,
    max_lookback_days: int = 30,
) -> pd.Timestamp:
    """Latest local-time date where wall-clock is past T_HOUR_LOCAL AND METAR/TAF/ASOS
    each have an obs ≥ T_utc AND HRRR covers the date.

    Returns a naive (tz-stripped), normalized pd.Timestamp.
    """
    if now_local is None:
        now_local = pd.Timestamp.now(tz=local_tz)

    metar_latest = pd.to_datetime(bundle.metar["timestamp"], utc=True).max()
    taf_latest = pd.to_datetime(bundle.taf["valid"], utc=True).max()
    asos_latest = pd.to_datetime(bundle.asos["timestamp"], utc=True).max()
    hrrr_dates = set(pd.to_datetime(bundle.hrrr["date"]).dt.normalize().unique())

    candidate = now_local.normalize()
    for _ in range(max_lookback_days + 1):
        anchor_t_local = candidate + pd.Timedelta(hours=t_hour_local)
        anchor_t_utc = anchor_t_local.tz_convert("UTC")
        past_T = now_local >= anchor_t_local
        iem_fresh = (
            metar_latest >= anchor_t_utc
            and taf_latest >= anchor_t_utc
            and asos_latest >= anchor_t_utc
        )
        hrrr_covered = candidate.tz_localize(None).normalize() in hrrr_dates
        if past_T and iem_fresh and hrrr_covered:
            return candidate.tz_localize(None).normalize()
        candidate -= pd.Timedelta(days=1)

    raise RuntimeError(
        f"No viable anchor in past {max_lookback_days} days. "
        f"METAR_max={metar_latest}, TAF_max={taf_latest}, ASOS_max={asos_latest}, "
        f"HRRR_max={max(hrrr_dates) if hrrr_dates else None}"
    )


def freshness_report(bundle: DataBundle, anchor_date: pd.Timestamp,
                     t_hour_local: int, local_tz: str) -> dict:
    """Per-source minutes of lag vs T_utc of `anchor_date`. For TUI display."""
    t_local = pd.Timestamp(anchor_date).normalize().tz_localize(local_tz) \
              + pd.Timedelta(hours=t_hour_local)
    t_utc = t_local.tz_convert("UTC")
    out = {}
    for name, df, tcol in [
        ("metar", bundle.metar, "timestamp"),
        ("taf",   bundle.taf,   "valid"),
        ("asos",  bundle.asos,  "timestamp"),
        ("cli",   bundle.cli,   "date"),
    ]:
        latest = pd.to_datetime(df[tcol], utc=(tcol != "date")).max()
        latest_utc = (pd.Timestamp(latest).tz_localize("UTC") if tcol == "date"
                      else pd.Timestamp(latest).tz_convert("UTC"))
        out[name] = (t_utc - latest_utc).total_seconds() / 60
    hrrr_max = pd.to_datetime(bundle.hrrr["date"]).max().normalize()
    out["hrrr"] = (pd.Timestamp(anchor_date).normalize() - hrrr_max).days * 24 * 60.0
    return out
