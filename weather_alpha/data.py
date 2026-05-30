"""Data layer — real-time in-process refresh, bundle load, anchor selection.

The hot path (`refresh_live`) calls `live_fetchers.fetch_all_live` once per
anchor cycle, then merges results into the existing parquet store via the same
incremental upsert pattern `refresh_data.py` uses. No subprocesses, no shell
overhead, full schema parity with the historical data the model was trained on.

Use the standalone scripts (`python refresh_data.py`, `python backfill_hrrr.py`)
only for cold-start historical backfill — the production engine never touches them.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from weather_alpha.live_fetchers import fetch_all_live

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DataBundle:
    metar: pd.DataFrame
    taf: pd.DataFrame
    asos: pd.DataFrame
    cli: pd.DataFrame
    hrrr: pd.DataFrame


# Per-source merge specs — must match the historical parquet dedupe columns
# from refresh_data.py (so live + historical rows interleave cleanly).
_MERGE_SPEC = {
    "metar": dict(ts_col="timestamp", dedupe_cols=["timestamp", "report_type", "mod"]),
    "taf":   dict(ts_col="valid",     dedupe_cols=["valid", "fx_valid", "ftype", "raw"]),
    "asos":  dict(ts_col="timestamp", dedupe_cols=["timestamp"]),
    "cli":   dict(ts_col="date",      dedupe_cols=["date"]),
    "hrrr":  dict(ts_col="valid_utc", dedupe_cols=["date", "fxx"]),
}


def _parquet_path(data_dir: Path, source: str, station: str) -> Path:
    if source == "hrrr":
        return data_dir / f"hrrr_12z_{station}.parquet"
    return data_dir / f"{source}_{station}.parquet"


def merge_into_parquet(source: str, new_df: pd.DataFrame, path: Path) -> tuple[int, int]:
    """Upsert `new_df` into the parquet at `path`. Returns (rows_added, total_after).

    Mirrors `refresh_data.py:_refresh`'s dedupe-and-merge pattern. Idempotent.
    Skips the disk write entirely when no new rows would land (cheap no-op for
    every-cycle refreshes that catch a stale upstream).
    """
    if new_df is None or new_df.empty:
        return 0, _count(path)

    spec = _MERGE_SPEC[source]
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        new_df.sort_values(spec["ts_col"]).reset_index(drop=True).to_parquet(path)
        return len(new_df), len(new_df)

    existing = pd.read_parquet(path)
    combined = pd.concat([existing, new_df], ignore_index=True, sort=False)
    combined = combined.drop_duplicates(subset=spec["dedupe_cols"], keep="last")
    added = len(combined) - len(existing)
    if added == 0:
        return 0, len(existing)
    combined = combined.sort_values(spec["ts_col"]).reset_index(drop=True)
    combined.to_parquet(path)
    return added, len(combined)


def _count(path: Path) -> int:
    return len(pd.read_parquet(path)) if path.exists() else 0


async def refresh_live(
    station: str,
    anchor_dt_local: pd.Timestamp,
    data_dir: Path,
    *,
    local_tz: str = "America/Chicago",
    asos_source: str = "iem",
    synoptic_token: str | None = None,
) -> dict[str, int]:
    """Fetch all 5 sources in parallel, merge into parquets. Returns per-source
    row counts after merge. ~3–5 s wall time on a healthy network.
    """
    t0 = time.perf_counter()
    fetched = await fetch_all_live(
        station=station, anchor_dt_local=anchor_dt_local,
        local_tz=local_tz, asos_source=asos_source,
        synoptic_token=synoptic_token, data_dir=data_dir,
    )
    added: dict[str, int] = {}
    for source, df in fetched.items():
        if df is None:
            added[source] = -1
            continue
        path = _parquet_path(data_dir, source, station)
        delta, _total = merge_into_parquet(source, df, path)
        added[source] = delta
    dt = time.perf_counter() - t0
    logger.info("refresh_live %.2fs  %s", dt,
                {k: (f"+{added[k]}" if added[k] >= 0 else "FAIL") for k in added})
    return added


def load_bundle(data_dir: Path, station: str) -> DataBundle:
    """Load all 5 parquets. Fail loudly on missing files."""
    paths = {src: _parquet_path(data_dir, src, station)
             for src in ("metar", "taf", "asos", "cli", "hrrr")}
    missing = [n for n, p in paths.items() if not p.exists()]
    if missing:
        raise FileNotFoundError(f"missing parquets: {missing}")
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
    """Latest local-day where wall-clock past T_HOUR_LOCAL AND METAR/TAF/ASOS each
    have an obs ≥ T_utc AND HRRR covers the date. Returns naive normalized ts.
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
        iem_fresh = (metar_latest >= anchor_t_utc
                     and taf_latest >= anchor_t_utc
                     and asos_latest >= anchor_t_utc)
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
