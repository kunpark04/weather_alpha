"""Feature builder — preserves the live_predict §2/§3 "zero drift by construction" trick.

We dynamically exec the §3.* code cells from `notebooks/model_v3.ipynb` into a controlled
namespace pre-seeded with the loaded parquets and constants. This guarantees the live
feature recipe is bit-identical to the training recipe — the same property that
live_predict.ipynb §3 provided.

Long-term: port the §3 cells into a normal Python module (`features_v3.py`) and validate
parity against this dynamic-exec output. Until then, this is the safe path.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from weather_alpha.data import DataBundle
from weather_alpha.model import ModelArtifacts

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeatureBuild:
    feature_df: pd.DataFrame
    target_df: pd.DataFrame
    anchor_date: pd.Timestamp


def _load_section3_cells(notebook_path: Path) -> list[tuple[int, str, str]]:
    """Scan notebook for "### §3.X" markdown headers; return (cell_idx, header, code)."""
    with notebook_path.open("r", encoding="utf-8") as f:
        nb = json.load(f)

    pairs: list[tuple[int, str, str]] = []
    for i, cell in enumerate(nb["cells"]):
        if cell["cell_type"] != "markdown":
            continue
        first_line = "".join(cell["source"]).split("\n", 1)[0].strip()
        if not first_line.startswith("### §3."):
            continue
        for j in range(i + 1, len(nb["cells"])):
            if nb["cells"][j]["cell_type"] == "code":
                pairs.append((j, first_line, "".join(nb["cells"][j]["source"])))
                break

    if not pairs:
        raise RuntimeError(f"No '### §3.' markdown headers in {notebook_path}")
    return pairs


def build_features(
    bundle: DataBundle,
    art: ModelArtifacts,
    anchor_date: pd.Timestamp,
    notebook_path: Path,
    target_window_days: int = 14,
    start_date: str = "01/01/2020",
) -> FeatureBuild:
    """Build the feature_df for `anchor_date` by dynamic-exec'ing model_v3.ipynb §3.*."""
    anchor_date = pd.Timestamp(anchor_date).normalize()

    # ---- target_df: cli truth + placeholder row for anchor if not yet settled
    cli = bundle.cli.copy()
    if pd.Timestamp(anchor_date) not in pd.to_datetime(cli["date"]).values:
        extra = pd.DataFrame({
            "station":    [art.station],
            "date":       [anchor_date],
            "max_temp_f": pd.array([pd.NA], dtype="Int64"),
        })
        cli = pd.concat([cli, extra], ignore_index=True).sort_values("date").reset_index(drop=True)

    target_df = (
        cli[["date", "max_temp_f"]]
        .rename(columns={"max_temp_f": art.target_col})
        .drop_duplicates(subset=["date"], keep="first")
        .sort_values("date")
        .reset_index(drop=True)
    )
    start_ts = pd.to_datetime(start_date, format="%m/%d/%Y")
    target_df = target_df[target_df["date"] >= start_ts].reset_index(drop=True)

    # ---- perf trim: only keep the recent window the model_v3 §3 cells will touch
    keep_start = pd.Timestamp(anchor_date) - pd.Timedelta(days=target_window_days)
    taf_window_start = (keep_start.tz_localize(art.local_tz) - pd.Timedelta(days=2)).tz_convert("UTC")
    taf = bundle.taf[bundle.taf["valid"] >= taf_window_start].reset_index(drop=True)
    target_df = target_df[target_df["date"] >= keep_start].reset_index(drop=True)

    # ---- namespace seeded with everything the §3 cells expect to see
    data_dir = Path(notebook_path).parent.parent / "data"
    ns: dict[str, Any] = {
        # parquets
        "metar":      bundle.metar,
        "taf":        taf,
        "asos":       bundle.asos,
        "cli":        cli,
        "hrrr":       bundle.hrrr,
        "target_df":  target_df,
        # paths used by §3 cells that re-read parquets directly
        "DATA_DIR":   data_dir,
        "Path":       Path,
        # constants — match the names the notebook expects
        "STATION":               art.station,
        "TARGET_COL":            art.target_col,
        "T_HOUR_LOCAL":          art.t_hour_local,
        "PEAK_START_HOUR_LOCAL": art.peak_start_hour_local,
        "PEAK_END_HOUR_LOCAL":   art.peak_end_hour_local,
        "LOCAL_TZ":              art.local_tz,
        "INTEGER_F_GRID":        art.integer_f_grid,
        "QUANTILES":             art.quantiles.tolist(),
        "FEATURE_COLS":          art.feature_cols,
        "SEASON_OF_MONTH":       art.season_of_month,
        "SEEDS":                 art.seeds,
        # libs available to the notebook cells
        "np":   np,
        "pd":   pd,
        "json": json,
    }

    cells = _load_section3_cells(notebook_path)
    logger.info("Exec'ing %d §3.* cells from %s", len(cells), notebook_path.name)
    t0 = time.perf_counter()
    for idx, header, code in cells:
        try:
            exec(compile(code, f"<{notebook_path.name}::cell{idx}>", "exec"), ns)
        except Exception:
            logger.exception("§3 cell exec failed: %s (cell %d)", header, idx)
            raise
    logger.info("§3 build complete in %.1fs", time.perf_counter() - t0)

    if "feature_df" not in ns:
        raise RuntimeError("§3 cells did not produce a `feature_df` global")

    feature_df: pd.DataFrame = ns["feature_df"]
    if not (feature_df["date"] == anchor_date).any():
        raise RuntimeError(
            f"feature_df has no row for anchor_date={anchor_date.date()}. "
            f"Rows present: {sorted(feature_df['date'].unique())[-5:]}"
        )

    return FeatureBuild(feature_df=feature_df, target_df=ns["target_df"], anchor_date=anchor_date)
