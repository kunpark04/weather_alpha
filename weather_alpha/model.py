"""Model artifact loader + `predict_for_anchor`. Ported from live_predict §1 + §4.2."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from weather_alpha.pmf import (
    INTEGER_F_GRID,
    apply_hard_floor,
    pmf_quantile,
    quantile_to_bucket_probs,
    recalibrate_row,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelArtifacts:
    name: str
    final_models: list                              # list[seeds] of list[quantiles] of LGBM Boosters
    isotonic_recalibrators: dict                    # {"DJF": iso, "MAM": iso, "JJA": iso, "SON": iso}
    feature_cols: list[str]
    quantiles: np.ndarray
    integer_f_grid: np.ndarray
    season_of_month: dict[int, str]
    seeds: list[int]
    station: str
    target_col: str
    t_hour_local: int
    peak_start_hour_local: int
    peak_end_hour_local: int
    local_tz: str
    has_hard_floor: bool
    use_calib_for_seasons: set[str]


def load_artifacts(model_dir: Path) -> ModelArtifacts:
    """Load v3 artifacts from `data/model_v3_artifacts/` (or analogous v4 dir)."""
    model_dir = Path(model_dir)
    if not model_dir.exists():
        raise FileNotFoundError(f"model dir not found: {model_dir}")

    with (model_dir / "constants.json").open("r", encoding="utf-8") as f:
        const: dict[str, Any] = json.load(f)
    if "HAS_HARD_FLOOR" not in const:
        raise RuntimeError(
            f"constants.json in {model_dir} missing HAS_HARD_FLOOR. "
            "Re-run model_v3.ipynb's final artifact-save cell."
        )

    final_models = joblib.load(model_dir / "final_models.joblib")
    recalibrators = joblib.load(model_dir / "isotonic_recalibrators.joblib")

    art = ModelArtifacts(
        name=const.get("MODEL_NAME", model_dir.name),
        final_models=final_models,
        isotonic_recalibrators=recalibrators,
        feature_cols=const["FEATURE_COLS"],
        quantiles=np.array(const["QUANTILES"]),
        integer_f_grid=np.array(const["INTEGER_F_GRID"], dtype=int),
        season_of_month={int(k): v for k, v in const["SEASON_OF_MONTH"].items()},
        seeds=list(const["SEEDS"]),
        station=const["STATION"],
        target_col=const["TARGET_COL"],
        t_hour_local=int(const["T_HOUR_LOCAL"]),
        peak_start_hour_local=int(const["PEAK_START_HOUR_LOCAL"]),
        peak_end_hour_local=int(const["PEAK_END_HOUR_LOCAL"]),
        local_tz=const["LOCAL_TZ"],
        has_hard_floor=bool(const["HAS_HARD_FLOOR"]),
        use_calib_for_seasons=set(const.get("USE_CALIB_FOR_SEASONS", ["DJF", "MAM", "JJA", "SON"])),
    )
    logger.info(
        "Loaded model %s — %d seeds × %d quantiles, hard_floor=%s, calib=%s, %d features",
        art.name, len(art.final_models), len(art.final_models[0]),
        art.has_hard_floor, sorted(art.use_calib_for_seasons), len(art.feature_cols),
    )
    return art


@dataclass(frozen=True)
class Prediction:
    date: pd.Timestamp
    model: str
    pmf: pd.Series                    # index = integer °F, values = P
    median: int
    lo10: int
    hi90: int
    peak_F: int
    peak_P: float
    season: str
    hard_floor: int | None
    leaked_below_floor: float
    nan_features: list[str] = field(default_factory=list)


def predict_for_anchor(art: ModelArtifacts, anchor_date, feature_df: pd.DataFrame) -> Prediction:
    """Score the loaded ensemble on the feature row for `anchor_date`."""
    target = pd.Timestamp(anchor_date).normalize()
    row_mask = feature_df["date"] == target
    if not row_mask.any():
        raise KeyError(f"No feature row for {target.date()} in feature_df")

    X = feature_df.loc[row_mask, art.feature_cols].astype("float64")
    nan_cols = X.columns[X.isna().any().values].tolist()
    if nan_cols:
        logger.warning("NaN in %d feature(s) for %s: %s", len(nan_cols), target.date(), nan_cols)

    q_levels = art.quantiles
    seed_pmfs = np.zeros((len(art.final_models), len(art.integer_f_grid)))
    for s_idx, seed_models in enumerate(art.final_models):
        q_preds = np.array([m.predict(X)[0] for m in seed_models])
        seed_pmfs[s_idx] = quantile_to_bucket_probs(q_levels, q_preds, art.integer_f_grid)
    raw_probs = seed_pmfs.mean(axis=0)
    if raw_probs.sum() > 0:
        raw_probs = raw_probs / raw_probs.sum()

    season_key = art.season_of_month[target.month]
    if season_key in art.use_calib_for_seasons:
        calib_probs = recalibrate_row(raw_probs, art.isotonic_recalibrators, season_key)
    else:
        calib_probs = raw_probs.copy()

    hard_floor_val: int | None = None
    leaked = 0.0
    if art.has_hard_floor and "running_max_F_T_hard" in feature_df.columns:
        hf_raw = feature_df.loc[row_mask, "running_max_F_T_hard"].iloc[0]
        if pd.notna(hf_raw):
            hard_floor_val = int(hf_raw)
            calib_probs, leaked = apply_hard_floor(calib_probs, hard_floor_val, art.integer_f_grid)

    pmf = pd.Series(calib_probs, index=art.integer_f_grid, name="P")

    return Prediction(
        date=target,
        model=art.name,
        pmf=pmf,
        median=pmf_quantile(calib_probs, 0.5, art.integer_f_grid),
        lo10=pmf_quantile(calib_probs, 0.10, art.integer_f_grid),
        hi90=pmf_quantile(calib_probs, 0.90, art.integer_f_grid),
        peak_F=int(pmf.idxmax()),
        peak_P=float(pmf.max()),
        season=season_key,
        hard_floor=hard_floor_val,
        leaked_below_floor=leaked,
        nan_features=nan_cols,
    )
