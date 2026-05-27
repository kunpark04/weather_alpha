"""Calibrate-to-market shrinkage (Tier 1.1).

The v3 LightGBM model is well-calibrated on weather truth (CRPS ~1.2 °F) but
miscalibrated against Kalshi market outcomes — its per-day p_top2 claim runs
~30pp above the realized hit rate on firing days. This module fits a single
shrinkage parameter α ∈ [0, 1] that blends model and market-implied bucket
probabilities so the resulting distribution minimizes negative log-likelihood
against observed Kalshi bucket outcomes:

    p_calibrated_bucket = α × p_model_bucket + (1 − α) × p_market_implied_bucket
                          (then re-normalize to sum to 1)

Usage in backtest: leave-one-out CV. For each day, fit α on all other days,
apply to that day's predictions. Honest by construction.

α near 1 → model dominates; α near 0 → market dominates. Empirically expected
α somewhere in the middle if both sources carry information.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
from scipy.optimize import minimize_scalar

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CalibrationDay:
    """One day's bucket data, in matched arrays."""
    date: object                          # pd.Timestamp
    tickers: list[str]
    model_probs: np.ndarray               # shape (n_buckets,)
    market_probs: np.ndarray              # shape (n_buckets,), normalized to sum 1
    outcome_idx: int                      # which bucket index won, -1 if no match


def _neg_log_lik(alpha: float, days: list[CalibrationDay]) -> float:
    """Negative log-likelihood of observed outcomes under p = α·model + (1−α)·market."""
    nll = 0.0
    for d in days:
        if d.outcome_idx < 0:
            continue
        blended = alpha * d.model_probs + (1.0 - alpha) * d.market_probs
        s = blended.sum()
        if s <= 0:
            return 1e9
        blended = blended / s
        p_win = max(float(blended[d.outcome_idx]), 1e-9)
        nll -= np.log(p_win)
    return nll


def fit_alpha(days: list[CalibrationDay]) -> float:
    """Fit a single α ∈ [0, 1] minimizing NLL across all days. Returns optimal α."""
    if not days:
        return 1.0
    result = minimize_scalar(
        lambda a: _neg_log_lik(a, days),
        bounds=(0.0, 1.0), method="bounded",
        options={"xatol": 1e-4},
    )
    return float(result.x)


def leave_one_out_calibrate(days: list[CalibrationDay]) -> tuple[list[np.ndarray], list[float]]:
    """Return (calibrated_probs_per_day, alpha_per_day). No look-ahead — for day i,
    α is fit excluding day i's observation."""
    n = len(days)
    if n == 0:
        return [], []

    calibrated: list[np.ndarray] = []
    alphas: list[float] = []
    for i in range(n):
        others = [days[j] for j in range(n) if j != i]
        alpha = fit_alpha(others)
        alphas.append(alpha)
        blended = alpha * days[i].model_probs + (1.0 - alpha) * days[i].market_probs
        s = blended.sum()
        calibrated.append(blended / s if s > 0 else blended.copy())
    logger.info("LOO calibration: α mean=%.3f median=%.3f std=%.3f (n=%d)",
                float(np.mean(alphas)), float(np.median(alphas)),
                float(np.std(alphas)), n)
    return calibrated, alphas


def build_override_lookup(days: list[CalibrationDay],
                          calibrated_probs: list[np.ndarray]) -> dict:
    """Build {date: {ticker: calibrated_p}} for use as strategy p_model_override."""
    out: dict = {}
    for d, cal in zip(days, calibrated_probs):
        out[d.date] = {t: float(p) for t, p in zip(d.tickers, cal)}
    return out
