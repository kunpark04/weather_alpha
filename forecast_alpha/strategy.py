"""Strategy engine — joint Kelly + KL-weighted concentration + regime throttle.

Reads a model Prediction + live Kalshi contracts + the feature row used to make the
prediction; emits a list of TargetPosition entries that the execution engine will
translate into orders.

The strategy is documented in detail in the Phase-1 paper-trade doc; the key
mathematical points:

  1) Per-contract Kelly. Each Kalshi bucket is an independent binary bet from the
     bot's POV (we always trade either YES or NO of a single side, never spread the
     same bucket). For a $1-payoff contract priced at `c`:
         Kelly fraction f* = (p - c) / (1 - c)        if p > c   (else 0)
     We always pick the side with the higher net-of-fee EV.

  2) KL-weighted concentration. We weight per-contract Kelly by that contract's
     contribution to KL(P_model || P_market). Buckets where our view is *most*
     surprising relative to the market get a larger share of capital.

  3) Fractional Kelly (kelly_fraction in config). We don't run full Kelly because:
     (a) our model probabilities have estimation error,
     (b) drawdowns under full Kelly can wipe a small bankroll.

  4) Regime throttle. The model has a known failure mode on regime-change days
     (HRRR upward correction misfires). We halve sizing when either:
       - |hrrr_t2m_max_peak − cli_high_yesterday| exceeds a config threshold, OR
       - the peak bucket probability is below a confidence floor.

  5) Risk caps. Two hard caps in config:
       - per-contract exposure as fraction of bankroll
       - total exposure as fraction of bankroll
     Both are applied AFTER Kelly + KL weighting; we scale the whole vector to fit.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np
import pandas as pd

from forecast_alpha.config import StrategyCfg
from forecast_alpha.fees import net_ev_cents, trade_fee_cents
from forecast_alpha.kalshi import KalshiContract
from forecast_alpha.model import Prediction
from forecast_alpha.pmf import MarketBucket, kl_per_bucket, market_implied_pmf

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TargetPosition:
    ticker: str
    side: str                                 # "yes" | "no"
    target_contracts: int
    limit_price_cents: int                    # the entry price we'd accept
    bucket_spec: str
    rationale: dict[str, float | str | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class StrategyOutput:
    targets: list[TargetPosition]
    diagnostics: dict[str, float | str]


def run_strategy(
    cfg: StrategyCfg,
    prediction: Prediction,
    contracts: list[KalshiContract],
    feature_row: pd.Series | None,
    bankroll_usd: float,
) -> StrategyOutput:
    """Top-level strategy entry. Returns target positions + diagnostics."""
    live = [c for c in contracts if c.is_live]
    if not live:
        return StrategyOutput(targets=[], diagnostics={"reason": "no live contracts"})

    market_buckets = _market_buckets(live, prediction)
    p_model = np.array([b.p_model for b in market_buckets], dtype=float)
    p_market = market_implied_pmf(market_buckets)
    kl_contrib = kl_per_bucket(p_model, p_market)
    kl_total = float(kl_contrib.sum())

    throttle = _regime_throttle(cfg.regime_throttle, prediction, feature_row)

    candidates: list[_Candidate] = []
    for bucket, p_m, p_q, kl_i in zip(live, p_model, p_market, kl_contrib):
        cand = _candidate_for(bucket, p_m, p_q, kl_i, cfg)
        if cand is not None:
            candidates.append(cand)

    if not candidates:
        return StrategyOutput(
            targets=[],
            diagnostics={
                "reason":      "no contract clears net-of-fee edge floor",
                "kl_total":    kl_total,
                "throttle":    throttle,
            },
        )

    # Weight per-candidate Kelly fraction by KL share (concentration) and apply throttle.
    kl_pos = max(sum(max(c.kl_share, 0.0) for c in candidates), 1e-9)
    for c in candidates:
        c.weight = max(c.kl_share, 0.0) / kl_pos        # 0..1, sums to 1 over positives
        c.scaled_kelly = c.kelly_f * (1 - cfg.kl_concentration_alpha + cfg.kl_concentration_alpha * c.weight)
        c.scaled_kelly *= cfg.kelly_fraction * throttle

    # Apply per-contract cap.
    for c in candidates:
        c.exposure_frac = min(c.scaled_kelly, cfg.per_contract_max_pct)

    # Apply total-exposure cap.
    total = sum(c.exposure_frac for c in candidates)
    if total > cfg.total_exposure_max_pct and total > 0:
        scale = cfg.total_exposure_max_pct / total
        for c in candidates:
            c.exposure_frac *= scale
        total = cfg.total_exposure_max_pct

    targets: list[TargetPosition] = []
    deployed = 0.0
    for c in candidates:
        notional_usd = c.exposure_frac * bankroll_usd
        if notional_usd <= 0:
            continue
        contracts_to_buy = int(math.floor(notional_usd / max(c.price_dollars, 0.01)))
        if contracts_to_buy <= 0:
            continue
        targets.append(TargetPosition(
            ticker=c.ticker,
            side=c.side,
            target_contracts=contracts_to_buy,
            limit_price_cents=int(round(c.price_dollars * 100)),
            bucket_spec=c.bucket_spec,
            rationale={
                "p_model":         float(c.p_model),
                "p_market":        float(c.p_market),
                "kl_share":        float(c.kl_share),
                "kelly_f":         float(c.kelly_f),
                "scaled_kelly":    float(c.scaled_kelly),
                "exposure_frac":   float(c.exposure_frac),
                "net_ev_cents":    float(c.net_ev_cents),
                "throttle":        float(throttle),
            },
        ))
        deployed += contracts_to_buy * c.price_dollars

    diagnostics = {
        "kl_total":       kl_total,
        "throttle":       throttle,
        "candidates":     len(candidates),
        "targets":        len(targets),
        "deployed_usd":   deployed,
        "deployed_frac":  deployed / max(bankroll_usd, 1e-9),
    }
    logger.info(
        "strategy: %d/%d candidates clear; deploy $%.2f (%.1f%% of bankroll); KL=%.4f throttle=%.2f",
        len(targets), len(live), deployed, 100 * deployed / max(bankroll_usd, 1e-9),
        kl_total, throttle,
    )
    return StrategyOutput(targets=targets, diagnostics=diagnostics)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

@dataclass
class _Candidate:
    ticker: str
    bucket_spec: str
    side: str
    price_dollars: float
    p_model: float
    p_market: float
    kl_share: float
    kelly_f: float
    net_ev_cents: float
    weight: float = 0.0
    scaled_kelly: float = 0.0
    exposure_frac: float = 0.0


def _market_buckets(contracts: list[KalshiContract], pred: Prediction) -> list[MarketBucket]:
    """Project Prediction.pmf onto each contract's bucket spec to get p_model per bucket."""
    from forecast_alpha.pmf import bucket_prob
    out: list[MarketBucket] = []
    pmf_vals = pred.pmf.values
    for c in contracts:
        p = bucket_prob(pmf_vals, c.bucket_spec)
        out.append(MarketBucket(
            spec=c.bucket_spec,
            yes_ask=c.yes_ask,
            no_ask=c.no_ask,
            yes_mid=c.yes_mid,
            no_mid=c.no_mid,
            p_model=p,
        ))
    return out


def _candidate_for(c: KalshiContract, p_model: float, p_market: float,
                   kl_share: float, cfg: StrategyCfg) -> _Candidate | None:
    """Pick the higher-net-EV side (YES vs NO) for this contract; return None if neither clears."""
    # YES: pay yes_ask to win $1 if bucket resolves true (probability = p_model)
    ev_yes = net_ev_cents(p_model, c.yes_ask, contracts=1)
    # NO: pay no_ask to win $1 if bucket resolves false (probability = 1 - p_model)
    ev_no = net_ev_cents(1.0 - p_model, c.no_ask, contracts=1)

    if ev_yes >= ev_no:
        side = "yes"
        price = c.yes_ask
        p_win = p_model
        net_ev = ev_yes
    else:
        side = "no"
        price = c.no_ask
        p_win = 1.0 - p_model
        net_ev = ev_no

    if net_ev < cfg.edge_floor_cents:
        return None

    kelly_f = _kelly_fraction(p_win, price)
    if kelly_f <= 0:
        return None

    return _Candidate(
        ticker=c.ticker,
        bucket_spec=c.bucket_spec,
        side=side,
        price_dollars=price,
        p_model=p_model,
        p_market=p_market,
        kl_share=float(kl_share),
        kelly_f=kelly_f,
        net_ev_cents=float(net_ev),
    )


def _kelly_fraction(p_win: float, cost_dollars: float) -> float:
    """Kelly for a $1-payoff bet at `cost_dollars`. Returns 0 if no edge."""
    if cost_dollars <= 0 or cost_dollars >= 1:
        return 0.0
    if p_win <= cost_dollars:
        return 0.0
    # b = net odds = (payout - cost) / cost
    b = (1.0 - cost_dollars) / cost_dollars
    q_lose = 1.0 - p_win
    return max(0.0, (p_win * b - q_lose) / b)


def _regime_throttle(throttle_cfg, prediction: Prediction,
                     feature_row: pd.Series | None) -> float:
    """Multiplier ∈ {1.0, 0.5, 0.25}. Halve once per active condition."""
    multiplier = 1.0
    if prediction.peak_P < throttle_cfg.confidence_floor_top1:
        multiplier *= 0.5
        logger.info("regime throttle: low confidence (peak_P=%.3f < %.3f)",
                    prediction.peak_P, throttle_cfg.confidence_floor_top1)

    if feature_row is not None:
        try:
            hrrr_max = feature_row.get("hrrr_t2m_max_peak")
            cli_yest = feature_row.get("cli_high_yesterday")
            if pd.notna(hrrr_max) and pd.notna(cli_yest):
                # hrrr_t2m_max_peak is in Kelvin; convert to °F for the gap test
                hrrr_max_f = (float(hrrr_max) - 273.15) * 9 / 5 + 32
                gap = abs(hrrr_max_f - float(cli_yest))
                if gap > throttle_cfg.hrrr_persistence_gap_max_f:
                    multiplier *= 0.5
                    logger.info(
                        "regime throttle: large HRRR-persistence gap (|%.1f - %.1f| = %.1f > %.1f)",
                        hrrr_max_f, float(cli_yest), gap, throttle_cfg.hrrr_persistence_gap_max_f,
                    )
        except Exception as e:
            logger.warning("regime throttle: feature lookup failed (%s)", e)

    return multiplier
