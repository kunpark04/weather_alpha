"""PMF utilities — quantile→bucket conversion, isotonic recalibration, bucket parsers,
KL divergence, market-implied PMF.

Ported from notebooks/live_predict.ipynb §4.1 / §4.4 / §5 with two additions used by the
strategy engine: `market_implied_pmf` and `kl_per_bucket`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np
import pandas as pd

INTEGER_F_GRID = np.arange(-30, 131, dtype=int)


def smooth_pmf(values: np.ndarray, sigma: float = 1.0) -> np.ndarray:
    """Gaussian-smooth a PMF, renormalize. sigma is in °F units (grid is integer °F)."""
    if sigma <= 0:
        return values
    from scipy.ndimage import gaussian_filter1d
    smoothed = gaussian_filter1d(values, sigma=float(sigma), mode="nearest")
    total = smoothed.sum()
    return smoothed / total if total > 0 else smoothed


def bucket_lower_bound(spec) -> float:
    """Numeric lower bound for ordering Kalshi bucket specs. Open-ended specs
    return ±inf so they sort to the extremes."""
    s = str(spec).strip()
    if s.startswith("<="):
        return -float("inf")
    if s.startswith("<"):
        return -float("inf")
    if s.startswith(">="):
        return float(s[2:])
    if s.startswith(">"):
        return float(s[1:])
    if "-" in s and not s.startswith("-"):
        parts = s.split("-")
        try:
            return float(parts[0])
        except ValueError:
            return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Quantile → integer-°F PMF (Chernozhukov rearrangement + piecewise-linear CDF)
# ---------------------------------------------------------------------------

def quantile_to_bucket_probs(
    q_levels: np.ndarray,
    q_preds: np.ndarray,
    F_grid: np.ndarray = INTEGER_F_GRID,
) -> np.ndarray:
    """Convert one row's quantile predictions into P(cli_high = F) for each F in F_grid.

    Sort quantiles to enforce monotonicity (Chernozhukov rearrangement), interpolate
    the (F, tau) pairs as a piecewise-linear CDF with tail anchors at
    (q_min - 10, 0) and (q_max + 10, 1), then take CDF(F+0.5) - CDF(F-0.5).
    """
    if np.isnan(q_preds).any():
        return np.full(len(F_grid), np.nan)
    q_sorted = np.sort(q_preds)
    F_anchor = np.concatenate([[q_sorted[0] - 10], q_sorted, [q_sorted[-1] + 10]])
    P_anchor = np.concatenate([[0.0], q_levels, [1.0]])
    cdf_hi = np.interp(F_grid + 0.5, F_anchor, P_anchor)
    cdf_lo = np.interp(F_grid - 0.5, F_anchor, P_anchor)
    probs = np.clip(cdf_hi - cdf_lo, 0.0, None)
    total = probs.sum()
    return probs / total if total > 0 else probs


def recalibrate_row(
    probs_row: np.ndarray,
    recalibrators: dict,
    season_key: str,
) -> np.ndarray:
    """Apply season-specific isotonic recalibration to a row's bucket PMF.

    Reads the CDF, maps it through the isotonic recalibrator, differences back.
    """
    if np.isnan(probs_row).any() or season_key not in recalibrators:
        return np.full(len(probs_row), np.nan)
    iso = recalibrators[season_key]
    cdf_raw = np.cumsum(probs_row)
    cdf_calib = iso.predict(cdf_raw)
    new_probs = np.empty(len(probs_row))
    new_probs[0] = cdf_calib[0]
    new_probs[1:] = np.diff(cdf_calib)
    new_probs = np.clip(new_probs, 0.0, None)
    s = new_probs.sum()
    return new_probs / s if s > 0 else new_probs


def apply_hard_floor(pmf_values: np.ndarray, floor_f: int | float,
                     F_grid: np.ndarray = INTEGER_F_GRID) -> tuple[np.ndarray, float]:
    """Zero out mass below `floor_f`, renormalize. Returns (new_pmf, leaked_mass)."""
    below = F_grid < int(floor_f)
    leaked = float(pmf_values[below].sum())
    out = pmf_values.copy()
    out[below] = 0.0
    s = out.sum()
    if s > 0:
        out = out / s
    return out, leaked


# ---------------------------------------------------------------------------
# Bucket spec parsing (matches model_v3 §6.x semantics)
# ---------------------------------------------------------------------------

BucketPredicate = Callable[[int], bool]


def parse_bucket(spec) -> tuple[BucketPredicate, str]:
    """Parse a bucket spec → (predicate, human description).

    Accepts int, (lo, hi) tuple, "<K"/"<=K"/">K"/">=K", or "lo-hi".
    """
    if isinstance(spec, (int, np.integer)):
        x = int(spec)
        return (lambda F, _x=x: F == _x), f"high = {x}°F"
    if isinstance(spec, tuple) and len(spec) == 2:
        lo, hi = int(spec[0]), int(spec[1])
        return (lambda F, _lo=lo, _hi=hi: _lo <= F <= _hi), f"{lo}-{hi}°F"
    if isinstance(spec, str):
        s = spec.strip()
        m = re.fullmatch(r">=\s*(-?\d+)", s)
        if m:
            t = int(m.group(1)); return (lambda F, _t=t: F >= _t), f"high >= {t}°F"
        m = re.fullmatch(r">\s*(-?\d+)", s)
        if m:
            t = int(m.group(1)); return (lambda F, _t=t: F > _t), f"above {t}°F"
        m = re.fullmatch(r"<=\s*(-?\d+)", s)
        if m:
            t = int(m.group(1)); return (lambda F, _t=t: F <= _t), f"high <= {t}°F"
        m = re.fullmatch(r"<\s*(-?\d+)", s)
        if m:
            t = int(m.group(1)); return (lambda F, _t=t: F < _t), f"below {t}°F"
        m = re.fullmatch(r"(-?\d+)\s*-\s*(-?\d+)", s)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            return (lambda F, _lo=lo, _hi=hi: _lo <= F <= _hi), f"{lo}-{hi}°F"
    raise ValueError(f"Cannot parse bucket spec: {spec!r}")


def parse_kalshi_subtitle(sub: str | None) -> str | None:
    """Parse a Kalshi KXHIGHCHI subtitle ("X° to Y°", "X° or below", etc.) → bucket spec.

    Returns None if the subtitle doesn't match any known pattern.
    """
    s = (sub or "").strip().lower()
    if not s:
        return None
    m = re.match(r"(-?\d+)\D+to\D+(-?\d+)", s)
    if m: return f"{m.group(1)}-{m.group(2)}"
    m = re.match(r"(-?\d+)\D+(?:or|and)\s+below", s)
    if m: return f"<={m.group(1)}"
    m = re.match(r"(-?\d+)\D+(?:or|and)\s+above", s)
    if m: return f">={m.group(1)}"
    m = re.match(r"below\D+(-?\d+)", s)
    if m: return f"<{m.group(1)}"
    m = re.match(r"above\D+(-?\d+)", s)
    if m: return f">{m.group(1)}"
    return None


def bucket_mask(spec, F_grid: np.ndarray = INTEGER_F_GRID) -> np.ndarray:
    """Boolean mask over F_grid selecting buckets that match `spec`."""
    pred, _ = parse_bucket(spec)
    return np.array([pred(int(f)) for f in F_grid], dtype=bool)


def bucket_prob(pmf_values: np.ndarray, spec, F_grid: np.ndarray = INTEGER_F_GRID) -> float:
    """Integrate the PMF over the °F range described by `spec`."""
    return float(pmf_values[bucket_mask(spec, F_grid)].sum())


# ---------------------------------------------------------------------------
# Market-implied PMF + divergences (strategy engine inputs)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketBucket:
    """One Kalshi contract's row, restated in PMF-aligned form."""
    spec: str
    yes_ask: float            # in dollars (0..1)
    no_ask: float             # in dollars (0..1)
    yes_mid: float
    no_mid: float
    p_model: float            # model probability for this bucket


def market_implied_pmf(buckets: Iterable[MarketBucket]) -> np.ndarray:
    """Normalize the yes_ask vector across the 6 (or N) Kalshi contracts.

    yes_ask is Kalshi's ask for YES, which is approximately the market's
    probability that the bucket resolves true (modulo spread + fee + dealer skew).
    We normalize so the implied probabilities sum to 1, treating the buckets as
    a partition.
    """
    asks = np.array([b.yes_ask for b in buckets], dtype=float)
    asks = np.clip(asks, 1e-6, 1.0)
    s = asks.sum()
    if s <= 0:
        return np.full(len(asks), 1.0 / len(asks))
    return asks / s


def kl_per_bucket(p_model: np.ndarray, p_market: np.ndarray) -> np.ndarray:
    """Per-bucket KL contribution `p_i * log(p_i / q_i)` with safe clamping."""
    p = np.clip(p_model, 1e-9, 1.0)
    q = np.clip(p_market, 1e-9, 1.0)
    return p * np.log(p / q)


def expected_value(pmf_values: np.ndarray, F_grid: np.ndarray = INTEGER_F_GRID) -> float:
    return float((pmf_values * F_grid).sum())


def pmf_quantile(pmf_values: np.ndarray, q: float,
                 F_grid: np.ndarray = INTEGER_F_GRID) -> int:
    cdf = np.cumsum(pmf_values)
    return int(F_grid[np.searchsorted(cdf, q)])
