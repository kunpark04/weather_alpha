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
from dataclasses import dataclass, field, replace
from typing import Iterable

import numpy as np
import pandas as pd

from weather_alpha.config import StrategyCfg
from weather_alpha.fees import net_ev_cents, trade_fee_cents
from weather_alpha.kalshi import KalshiContract
from weather_alpha.model import Prediction
from weather_alpha.pmf import MarketBucket, kl_per_bucket, market_implied_pmf

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
    from weather_alpha.pmf import bucket_prob
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


def run_two_bucket_arbitrage(
    cfg: StrategyCfg,
    prediction: Prediction,
    contracts: list[KalshiContract],
    feature_row: pd.Series | None,
    bankroll_usd: float,
    *,
    min_margin: float = 0.05,
    fire_mode: str = "ev_gate",
    base_rate: float = 0.739,
    smooth_sigma: float = 0.0,
    force_adjacency: bool = False,
    p_model_override: dict[str, float] | None = None,
) -> StrategyOutput:
    """Two-bucket arbitrage on modal + second-modal.

    Bets both buckets such that whichever of the two wins, net is positive.

    fire_mode controls the gate:
      "ev_gate" (default): fires when p_top2 > sum_asks + min_margin
          → trusts the model's per-day claim of top-2 probability
      "arb_only": fires whenever sum_asks < 1 (any arbitrage available),
          uses base_rate (~74% historical top-2 hit rate) for Kelly sizing
          → treats every arb opportunity as a play on the unconditional base rate

    Sizing is equal-payout (X_i ∝ c_i) so the win dollar is identical regardless of
    which of the two legs hits. Total stake is Kelly-sized.
    """
    live = [c for c in contracts if c.is_live]
    if len(live) < 2:
        return StrategyOutput(targets=[], diagnostics={"reason": "fewer than 2 live contracts"})

    from weather_alpha.pmf import bucket_lower_bound, bucket_prob, smooth_pmf

    # Tier 1.3: optionally smooth the PMF before bucketing (kills quantile artifacts).
    pmf_values = prediction.pmf.values
    if smooth_sigma > 0:
        pmf_values = smooth_pmf(pmf_values, sigma=smooth_sigma)

    def p_for(c: KalshiContract) -> float:
        if p_model_override is not None and c.ticker in p_model_override:
            return float(p_model_override[c.ticker])
        return bucket_prob(pmf_values, c.bucket_spec)

    scored = sorted([(c, p_for(c)) for c in live], key=lambda x: -x[1])
    modal_c, modal_p = scored[0]

    if force_adjacency:
        # Tier 1.2: pick rank-2 from buckets adjacent to modal in Kalshi-position order,
        # not from raw PMF rank. Kills non-physical bimodal cases.
        sorted_by_pos = sorted(live, key=lambda c: bucket_lower_bound(c.bucket_spec))
        try:
            modal_pos = sorted_by_pos.index(modal_c)
        except ValueError:
            modal_pos = 0
        neighbors: list[tuple[KalshiContract, float]] = []
        if modal_pos > 0:
            n = sorted_by_pos[modal_pos - 1]
            neighbors.append((n, p_for(n)))
        if modal_pos < len(sorted_by_pos) - 1:
            n = sorted_by_pos[modal_pos + 1]
            neighbors.append((n, p_for(n)))
        if not neighbors:
            return StrategyOutput(targets=[], diagnostics={
                "reason": "modal has no adjacent buckets in layout"})
        neighbors.sort(key=lambda x: -x[1])
        second_c, second_p = neighbors[0]
    else:
        second_c, second_p = scored[1]

    c1 = modal_c.yes_ask
    c2 = second_c.yes_ask
    sum_asks = c1 + c2
    p_top2 = modal_p + second_p

    diag_base = {
        "strategy":   "two_bucket_arbitrage",
        "modal":      modal_c.bucket_spec,
        "second":     second_c.bucket_spec,
        "c1":         c1,
        "c2":         c2,
        "sum_asks":   sum_asks,
        "modal_p":    modal_p,
        "second_p":   second_p,
        "p_top2":     p_top2,
    }

    if sum_asks >= 1.0:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"no arbitrage (sum_asks={sum_asks:.3f} >= 1)"})

    if fire_mode == "ev_gate":
        if p_top2 <= sum_asks + min_margin:
            return StrategyOutput(targets=[], diagnostics={
                **diag_base, "reason": f"EV margin too thin (need p_top2 > {sum_asks + min_margin:.3f}, got {p_top2:.3f})"})
        p_used = p_top2
    elif fire_mode == "arb_only":
        # Trust the base rate regardless of per-day model claim. Still need sum_asks < base_rate
        # for positive Kelly, else skip.
        if base_rate <= sum_asks:
            return StrategyOutput(targets=[], diagnostics={
                **diag_base, "reason": f"sum_asks {sum_asks:.3f} >= base_rate {base_rate:.3f}"})
        p_used = base_rate
    else:
        raise ValueError(f"unknown fire_mode: {fire_mode!r}")

    throttle = _regime_throttle(cfg.regime_throttle, prediction, feature_row)
    if throttle <= 0:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": "throttle zeroed"})

    # Binary Kelly. Win-return = (1 - sum_asks)/sum_asks per dollar staked. Loss = -1.
    b = (1.0 - sum_asks) / sum_asks
    q = 1.0 - p_used
    kelly_full = max(0.0, (b * p_used - q) / b)
    f_stake = kelly_full * cfg.kelly_fraction * throttle
    f_stake = min(f_stake, cfg.total_exposure_max_pct)
    if f_stake <= 0:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"kelly <= 0 (kelly_full={kelly_full:.4f})", "throttle": throttle})

    total_stake_usd = f_stake * bankroll_usd
    K_usd = total_stake_usd / sum_asks               # equal payout target

    # Per-leg stakes (dollars) and contract counts
    X1 = K_usd * c1
    X2 = K_usd * c2
    n1 = max(0, int(X1 / c1))                        # = floor(K_usd) for both, equal payout
    n2 = max(0, int(X2 / c2))

    # Per-contract exposure cap
    max_per_contract_usd = cfg.per_contract_max_pct * bankroll_usd
    n1 = min(n1, int(max_per_contract_usd / c1))
    n2 = min(n2, int(max_per_contract_usd / c2))
    if n1 == 0 or n2 == 0:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": "size below 1 contract after caps", "throttle": throttle})

    edge_per_stake_cents = (1.0 - sum_asks) * 100   # gross
    targets = [
        TargetPosition(
            ticker=modal_c.ticker, side="yes",
            target_contracts=n1, limit_price_cents=int(round(c1 * 100)),
            bucket_spec=modal_c.bucket_spec,
            rationale={
                "role":           "modal",
                "p_model":        float(modal_p),
                "p_market":       float(c1),
                "kelly_f":        float(kelly_full),
                "scaled_kelly":   float(f_stake),
                "exposure_frac":  float(n1 * c1 / max(bankroll_usd, 1e-9)),
                "net_ev_cents":   float(edge_per_stake_cents),
                "throttle":       float(throttle),
            },
        ),
        TargetPosition(
            ticker=second_c.ticker, side="yes",
            target_contracts=n2, limit_price_cents=int(round(c2 * 100)),
            bucket_spec=second_c.bucket_spec,
            rationale={
                "role":           "second_modal",
                "p_model":        float(second_p),
                "p_market":       float(c2),
                "kelly_f":        float(kelly_full),
                "scaled_kelly":   float(f_stake),
                "exposure_frac":  float(n2 * c2 / max(bankroll_usd, 1e-9)),
                "net_ev_cents":   float(edge_per_stake_cents),
                "throttle":       float(throttle),
            },
        ),
    ]
    logger.info(
        "two_bucket_arb: modal=%s@%.2f (p=%.2f), 2nd=%s@%.2f (p=%.2f); sum=%.3f p_top2=%.3f kelly=%.3f throttle=%.2f",
        modal_c.bucket_spec, c1, modal_p, second_c.bucket_spec, c2, second_p,
        sum_asks, p_top2, kelly_full, throttle,
    )
    return StrategyOutput(
        targets=targets,
        diagnostics={
            **diag_base,
            "kelly_full":    kelly_full,
            "f_stake":       f_stake,
            "throttle":      throttle,
            "deployed_usd":  n1 * c1 + n2 * c2,
            "K_payout_usd":  K_usd,
            "edge_per_stake": (1.0 - sum_asks) / sum_asks,
        },
    )


def run_wing_strategy(
    cfg: StrategyCfg,
    prediction: Prediction,
    contracts: list[KalshiContract],
    feature_row: pd.Series | None,
    bankroll_usd: float,
    *,
    max_ask_sum: float = 0.97,
    min_ev_margin: float = 0.0,
    require_agreement: bool = True,
    assumed_win_prob: float | None = None,
    sizing_mode: str = "equal_payout",
    drop_worst_leg: bool = False,
    drop_lower_ask: bool = False,
    drop_higher_ask: bool = False,
    market_signal_power: float = 3.0,
    wing_anchor: str = "model",
    flat_stake_usd: float | None = None,
    fee_aware: bool = False,
) -> StrategyOutput:
    """Wing strategy: cover modal + adjacents, optionally drop one adjacent.

    Concept: trade AGREEMENT days (model & market modal match) using a coverage
    wing to capture the ~84% within-2F hit rate at a small per-trade edge.
    Many small wins, occasional larger losses on outlier truth.

    Sizing: equal-payout (K*ask per leg) by default - whichever leg wins pays
    the same gross. Kelly on the binary "in wing or not" bet.
    """
    from weather_alpha.pmf import bucket_lower_bound, bucket_prob

    live = [c for c in contracts if c.is_live]
    if len(live) < 3:
        return StrategyOutput(targets=[], diagnostics={"reason": "need >= 3 live contracts"})

    pmf_values = prediction.pmf.values
    scored = sorted(
        [(c, bucket_prob(pmf_values, c.bucket_spec)) for c in live],
        key=lambda x: -x[1],
    )
    model_modal_c, _ = scored[0]
    market_modal_c = max(live, key=lambda c: c.yes_ask)
    agreement = model_modal_c.ticker == market_modal_c.ticker

    diag_base = {
        "strategy":     "wing",
        "agreement":    agreement,
        "model_modal":  model_modal_c.bucket_spec,
        "market_modal": market_modal_c.bucket_spec,
    }

    if require_agreement and not agreement:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": "model-market disagreement (require_agreement=True)"})

    # Pick wing anchor (center): model_modal (default) or market_modal.
    if wing_anchor == "market":
        anchor_c = market_modal_c
    elif wing_anchor == "model":
        anchor_c = model_modal_c
    else:
        raise ValueError(f"unknown wing_anchor: {wing_anchor!r}")
    diag_base["wing_anchor"] = wing_anchor

    # Find anchor's position in the layout
    sorted_by_pos = sorted(live, key=lambda c: bucket_lower_bound(c.bucket_spec))
    try:
        modal_pos = sorted_by_pos.index(anchor_c)
    except ValueError:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": "anchor not found in sorted layout"})

    # Build wing: anchor + both positional adjacents
    wing: list[KalshiContract] = [anchor_c]
    if modal_pos > 0:
        wing.append(sorted_by_pos[modal_pos - 1])
    if modal_pos < len(sorted_by_pos) - 1:
        wing.append(sorted_by_pos[modal_pos + 1])

    # For 3-leg wing concept, require both adjacents. For drop_X variants, a single
    # adjacent at the edge is fine: the trade becomes [modal, the_one_adj] (no
    # market-adjacency choice to make, just pure coverage).
    allow_2leg = drop_lower_ask or drop_higher_ask or drop_worst_leg
    min_wing_size = 2 if allow_2leg else 3
    if len(wing) < min_wing_size:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"modal at edge of layout (wing size {len(wing)} < {min_wing_size})"})

    # drop_worst_leg requires the ORIGINAL 3-leg wing to clear max_ask_sum first.
    # Without this gate, dropping a leg lets the strategy fire on days where the
    # full wing wouldn't have, and those days lose money on average.
    if drop_worst_leg:
        sum_full = sum(c.yes_ask for c in wing)
        if sum_full >= max_ask_sum:
            return StrategyOutput(targets=[], diagnostics={
                **diag_base, "reason": f"pre-drop sum {sum_full:.3f} >= max {max_ask_sum}",
                "sum_full": sum_full,
            })
        if len(wing) > 2:
            edges = [(c, bucket_prob(pmf_values, c.bucket_spec) - c.yes_ask) for c in wing]
            worst_c, worst_edge = min(edges, key=lambda x: x[1])
            wing = [c for c in wing if c is not worst_c]

    # drop_lower_ask: keep modal + higher-ask adjacent.
    # Leans into the market's adjacency-direction signal (empirically ~82% accurate
    # on the 22-day sample, but tail-risky when wrong).
    if drop_lower_ask and len(wing) > 2:
        adjacents = [c for c in wing if c is not anchor_c]
        if len(adjacents) >= 2:
            adjacents.sort(key=lambda c: -c.yes_ask)
            higher_ask_adj = adjacents[0]
            wing = [anchor_c, higher_ask_adj]

    # drop_higher_ask: PLACEBO. Keep anchor + lower-ask adjacent.
    # Should perform terribly if the adjacency signal is real.
    if drop_higher_ask and len(wing) > 2:
        adjacents = [c for c in wing if c is not anchor_c]
        if len(adjacents) >= 2:
            adjacents.sort(key=lambda c: c.yes_ask)
            lower_ask_adj = adjacents[0]
            wing = [anchor_c, lower_ask_adj]

    sum_asks = sum(c.yes_ask for c in wing)
    p_top_wing = sum(bucket_prob(pmf_values, c.bucket_spec) for c in wing)

    # assumed_win_prob overrides the model's per-day p_top_wing for Kelly sizing.
    # Use this when you have a more reliable empirical hit-rate (e.g., 99% on
    # agreement+wing days) than the model's per-day confidence.
    p_used = assumed_win_prob if assumed_win_prob is not None else p_top_wing
    ev_margin = p_used - sum_asks

    diag_base.update({
        "sum_asks":     sum_asks,
        "p_top_wing":   p_top_wing,
        "p_used":       p_used,
        "ev_margin":    ev_margin,
    })

    if sum_asks >= max_ask_sum:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"sum_asks {sum_asks:.3f} >= max {max_ask_sum}"})

    if ev_margin < min_ev_margin:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"EV margin {ev_margin:.3f} < min {min_ev_margin}"})

    throttle = _regime_throttle(cfg.regime_throttle, prediction, feature_row)
    if throttle <= 0:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": "throttle zeroed"})

    # Sizing: flat-$ total (overrides Kelly) OR binary Kelly on the wing-or-miss bet.
    if flat_stake_usd is not None and flat_stake_usd > 0:
        total_stake_usd = min(float(flat_stake_usd), cfg.total_exposure_max_pct * bankroll_usd)
        f_stake = total_stake_usd / max(bankroll_usd, 1e-9)
        kelly_full = float("nan")                 # flat-sized, not Kelly (throttle bypassed)
    else:
        b = (1.0 - sum_asks) / sum_asks
        q = 1.0 - p_used
        kelly_full = max(0.0, (b * p_used - q) / b)
        f_stake = min(
            kelly_full * cfg.kelly_fraction * throttle,
            cfg.total_exposure_max_pct,
        )
        if f_stake <= 0:
            return StrategyOutput(targets=[], diagnostics={
                **diag_base, "reason": "kelly <= 0", "throttle": throttle})
        total_stake_usd = f_stake * bankroll_usd

    K = total_stake_usd / sum_asks                # payout target per leg (equal-payout interpretation)
    max_per_contract_usd = cfg.per_contract_max_pct * bankroll_usd

    # Compute per-leg stake based on sizing mode
    p_legs = [bucket_prob(pmf_values, c.bucket_spec) for c in wing]
    if sizing_mode == "prob_weighted":
        sum_p = sum(p_legs) or 1.0
        leg_stake_usd = [total_stake_usd * (p / sum_p) for p in p_legs]
    elif sizing_mode == "equal_payout":
        # Equal payout: x_i = K * c_i  (yields n_i = K for all i).
        # This is power=1 market-weighted: implicit ~67/33 split between adjacents.
        leg_stake_usd = [K * c.yes_ask for c in wing]
    elif sizing_mode == "market_weighted":
        # Amplified market signal: x_i proportional to (yes_ask_i)^power.
        # power=1 == equal-payout. power=3 yields ~82/18 split between two adjacents
        # (matching the empirical 82% market-adjacency accuracy).
        weights = [(c.yes_ask) ** market_signal_power for c in wing]
        sum_w = sum(weights) or 1.0
        leg_stake_usd = [total_stake_usd * (w / sum_w) for w in weights]
    else:
        raise ValueError(f"unknown sizing_mode: {sizing_mode!r}")

    targets: list[TargetPosition] = []
    for c, p_model_this, stake_usd in zip(wing, p_legs, leg_stake_usd):
        n = int(stake_usd / max(c.yes_ask, 0.01))
        n_cap = int(max_per_contract_usd / max(c.yes_ask, 0.01))
        n = min(n, n_cap)
        if n <= 0:
            continue
        targets.append(TargetPosition(
            ticker=c.ticker, side="yes", target_contracts=n,
            limit_price_cents=int(round(c.yes_ask * 100)),
            bucket_spec=c.bucket_spec,
            rationale={
                "strategy":      "wing",
                "role":          "anchor" if c == anchor_c else "wing",
                "sizing_mode":   sizing_mode,
                "p_model":       float(p_model_this),
                "p_market":      float(c.yes_ask / sum_asks),
                "kelly_f":       float(kelly_full),
                "scaled_kelly":  float(f_stake),
                "exposure_frac": float(n * c.yes_ask / max(bankroll_usd, 1e-9)),
                "confidence":    float(p_top_wing),
                "throttle":      float(throttle),
                "agreement":     agreement,
                "ev_margin":     float(ev_margin),
            },
        ))

    # Fee-aware gate (small accounts): skip if Kalshi fees would eat the entire win even
    # on the best-case leg — i.e. the trade cannot profit on any outcome.
    if fee_aware and targets:
        from weather_alpha.fees import trade_fee_cents
        total_fee_c = sum(trade_fee_cents(t.limit_price_cents / 100.0, t.target_contracts) for t in targets)
        total_cost_c = sum(t.target_contracts * t.limit_price_cents for t in targets)
        best_win_payout_c = max(t.target_contracts for t in targets) * 100
        if best_win_payout_c - total_cost_c - total_fee_c <= 0:
            return StrategyOutput(targets=[], diagnostics={
                **diag_base, "reason": "fee-aware: fees would eat the win",
                "total_fee_cents": total_fee_c})

    min_legs = 2 if (drop_worst_leg or drop_lower_ask or drop_higher_ask) else 3
    if len(targets) < min_legs:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"only {len(targets)} legs after caps (need >= {min_legs})"})

    logger.info(
        "wing(%s): anchor=%s sum_asks=%.3f p_top_wing=%.3f ev=%.3f kelly=%.3f throttle=%.2f",
        wing_anchor, anchor_c.bucket_spec, sum_asks, p_top_wing, ev_margin, kelly_full, throttle,
    )
    return StrategyOutput(targets=targets, diagnostics={
        **diag_base, "n_targets": len(targets),
        "kelly_full":     kelly_full,
        "f_stake":        f_stake,
        "throttle":       throttle,
        "deployed_usd":   sum(int(t.target_contracts * t.limit_price_cents / 100) for t in targets),
    })


def run_hard_floor_strategy(
    cfg: StrategyCfg,
    prediction: Prediction,
    contracts: list[KalshiContract],
    feature_row: pd.Series | None,
    bankroll_usd: float,
) -> StrategyOutput:
    """Buy NO on buckets that are MATHEMATICALLY IMPOSSIBLE at the anchor.

    Daily max is monotonically non-decreasing. If `running_max_F_T_hard` (the
    METAR-derived running maximum through 1 PM) already exceeds a bucket's
    upper bound, the bucket cannot possibly resolve YES. Buy NO at no_ask < $1
    for guaranteed payoff. Empirically verified: 0 violations in 2,288 OOF
    days, so this is structural alpha, not probability.

    Sizing: capped at per_contract_max_pct (NOT Kelly — Kelly says go all-in
    on certain outcomes, which has unacceptable concentration risk despite
    being correct probabilistically).
    """
    from weather_alpha.pmf import bucket_upper_bound

    if feature_row is None:
        return StrategyOutput(targets=[], diagnostics={"reason": "no feature_row"})
    rm = feature_row.get("running_max_F_T_hard")
    if rm is None or pd.isna(rm):
        return StrategyOutput(targets=[], diagnostics={"reason": "running_max NaN"})
    rm_F = int(rm)

    live = [c for c in contracts if c.is_live]
    if not live:
        return StrategyOutput(targets=[], diagnostics={"reason": "no live contracts"})

    targets: list[TargetPosition] = []
    impossible_specs: list[str] = []
    for c in live:
        ub = bucket_upper_bound(c.bucket_spec)
        if ub is None:                            # open-ended high tail — never impossible from below
            continue
        if rm_F <= ub:                            # not impossible
            continue
        impossible_specs.append(c.bucket_spec)

        # Net edge per contract: pay no_ask, receive $1, minus fee
        if c.no_ask >= 1.0:
            continue                              # can't profit
        gross_per_contract_cents = (1.0 - c.no_ask) * 100

        # Cap sizing at per-contract max regardless of "certainty" (Kelly says all-in).
        max_usd = cfg.per_contract_max_pct * bankroll_usd
        n = int(max_usd / max(c.no_ask, 0.01))
        if n <= 0:
            continue

        fee = trade_fee_cents(c.no_ask, n)
        net_cents = gross_per_contract_cents * n - fee
        if net_cents <= 0:
            continue

        targets.append(TargetPosition(
            ticker=c.ticker, side="no", target_contracts=n,
            limit_price_cents=int(round(c.no_ask * 100)),
            bucket_spec=c.bucket_spec,
            rationale={
                "strategy":         "hard_floor",
                "running_max_F":    rm_F,
                "bucket_upper_F":   ub,
                "margin_F":         rm_F - ub,
                "no_ask":           c.no_ask,
                "structural_arb":   True,
                "confidence":       1.0,                  # certain outcome → hold to settlement
                "expected_net_cents": float(net_cents),
                "exposure_frac":    float(n * c.no_ask / max(bankroll_usd, 1e-9)),
            },
        ))

    return StrategyOutput(targets=targets, diagnostics={
        "strategy":          "hard_floor",
        "running_max_F":     rm_F,
        "n_impossible":      len(impossible_specs),
        "impossible_specs":  ",".join(impossible_specs) if impossible_specs else "none",
        "n_targets":         len(targets),
    })


def run_variance_strategy(
    cfg: StrategyCfg,
    prediction: Prediction,
    contracts: list[KalshiContract],
    feature_row: pd.Series | None,
    bankroll_usd: float,
    *,
    var_diff_threshold_F2: float = 4.0,
) -> StrategyOutput:
    """Bet on dispersion difference between model PMF and market-implied PMF.

    Compute variance of each over the 6 bucket midpoints. If model_var <
    market_var - threshold (model is tighter), market is over-pricing
    uncertainty: BUY NO on wings, BUY YES on body. If model_var > market_var +
    threshold (model is wider), do the opposite.

    Unlike joint-Kelly per-bucket, this is a SHAPE/MOMENT bet — fires only
    when the aggregate distribution shape differs, and trades the wings
    against the body as a structured spread.
    """
    from weather_alpha.pmf import bucket_midpoint, bucket_prob

    live = [c for c in contracts if c.is_live]
    if len(live) < 4:
        return StrategyOutput(targets=[], diagnostics={"reason": "need >= 4 contracts"})

    pmf_vals = prediction.pmf.values

    # Per-contract model + market probs
    rows = []
    for c in live:
        p_model = bucket_prob(pmf_vals, c.bucket_spec)
        rows.append({
            "c":         c,
            "spec":      c.bucket_spec,
            "midpoint":  bucket_midpoint(c.bucket_spec),
            "p_model":   p_model,
            "yes_ask":   c.yes_ask,
            "no_ask":    c.no_ask,
        })

    # Normalize market probs (yes_ask sums slightly above 1 due to overround)
    yes_asks = np.array([r["yes_ask"] for r in rows])
    p_market = yes_asks / yes_asks.sum()
    p_model_arr = np.array([r["p_model"] for r in rows])
    midpoints = np.array([r["midpoint"] for r in rows])

    # Compute moments (over the 6-point discretization for fair comparison)
    model_mean = (p_model_arr * midpoints).sum()
    model_var = (p_model_arr * (midpoints - model_mean) ** 2).sum()
    market_mean = (p_market * midpoints).sum()
    market_var = (p_market * (midpoints - market_mean) ** 2).sum()
    var_diff = model_var - market_var

    diag_base = {
        "strategy":     "variance",
        "model_mean":   float(model_mean),
        "model_var":    float(model_var),
        "market_mean":  float(market_mean),
        "market_var":   float(market_var),
        "var_diff":     float(var_diff),
    }

    if abs(var_diff) < var_diff_threshold_F2:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"|var_diff|={abs(var_diff):.2f} < threshold {var_diff_threshold_F2}"})

    # Identify wings vs body: tail buckets are the "<=K" and ">=K+7" contracts
    is_wing = [str(r["spec"]).startswith("<") or str(r["spec"]).startswith(">") for r in rows]

    # Direction: var_diff < 0 → model is TIGHTER → market over-prices wings
    #            → SELL wings (BUY NO on wings) + BUY YES on body
    #            var_diff > 0 → model is WIDER  → market under-prices wings
    #            → BUY YES on wings + BUY NO on body
    if var_diff < 0:
        wing_side, body_side = "no", "yes"
    else:
        wing_side, body_side = "yes", "no"

    throttle = _regime_throttle(cfg.regime_throttle, prediction, feature_row)
    targets: list[TargetPosition] = []
    deployed = 0.0

    for r, wing in zip(rows, is_wing):
        c = r["c"]
        side = wing_side if wing else body_side
        price = c.yes_ask if side == "yes" else c.no_ask
        p_win = r["p_model"] if side == "yes" else (1.0 - r["p_model"])
        net_ev = net_ev_cents(p_win, price, contracts=1)
        if net_ev < cfg.edge_floor_cents:
            continue

        kelly = _kelly_fraction(p_win, price)
        f_stake = min(
            kelly * cfg.kelly_fraction * throttle,
            cfg.per_contract_max_pct,
        )
        if f_stake <= 0:
            continue
        n = int(f_stake * bankroll_usd / max(price, 0.01))
        if n <= 0:
            continue

        targets.append(TargetPosition(
            ticker=c.ticker, side=side, target_contracts=n,
            limit_price_cents=int(round(price * 100)),
            bucket_spec=c.bucket_spec,
            rationale={
                "strategy":      "variance",
                "role":          "wing" if wing else "body",
                "var_diff":      float(var_diff),
                "p_model":       float(r["p_model"]),
                "p_market":      float(p_market[rows.index(r)]),
                "net_ev_cents":  float(net_ev),
                "kelly_f":       float(kelly),
                "scaled_kelly":  float(f_stake),
                "exposure_frac": float(n * price / max(bankroll_usd, 1e-9)),
                "throttle":      float(throttle),
            },
        ))
        deployed += n * price

    # Total exposure cap
    cap = cfg.total_exposure_max_pct * bankroll_usd
    if deployed > cap and deployed > 0:
        scale = cap / deployed
        targets = [replace(t, target_contracts=max(0, int(t.target_contracts * scale)))
                   for t in targets]
        targets = [t for t in targets if t.target_contracts > 0]

    return StrategyOutput(targets=targets, diagnostics={
        **diag_base, "n_targets": len(targets), "throttle": throttle,
        "wing_side": wing_side, "body_side": body_side,
    })


def run_tail_probability_strategy(
    cfg: StrategyCfg,
    prediction: Prediction,
    contracts: list[KalshiContract],
    feature_row: pd.Series | None,
    bankroll_usd: float,
    *,
    min_net_ev_cents: float = 2.0,
) -> StrategyOutput:
    """Single-leg directional bets on the two TAIL buckets only (`<=K` and `>=K+7`).

    For each tail contract: compute model's tail probability vs market ask. Bet
    whichever side (YES / NO) has positive net-of-fee EV. Skips body buckets
    entirely — that's where market is most efficient. Targets the model's
    documented strength: tail probability calibration via climatology + hard floor.
    """
    from weather_alpha.pmf import bucket_prob

    live = [c for c in contracts if c.is_live]
    if not live:
        return StrategyOutput(targets=[], diagnostics={"reason": "no live contracts"})

    tails = [c for c in live if str(c.bucket_spec).strip().startswith(("<", ">"))]
    if not tails:
        return StrategyOutput(targets=[], diagnostics={"reason": "no tail contracts"})

    throttle = _regime_throttle(cfg.regime_throttle, prediction, feature_row)
    targets: list[TargetPosition] = []
    deployed_usd = 0.0

    for c in tails:
        p_model = bucket_prob(prediction.pmf.values, c.bucket_spec)
        ev_yes = net_ev_cents(p_model, c.yes_ask, contracts=1)
        ev_no = net_ev_cents(1.0 - p_model, c.no_ask, contracts=1)

        if ev_yes >= ev_no and ev_yes >= min_net_ev_cents:
            side, price, p_win, ev = "yes", c.yes_ask, p_model, ev_yes
        elif ev_no > ev_yes and ev_no >= min_net_ev_cents:
            side, price, p_win, ev = "no", c.no_ask, 1.0 - p_model, ev_no
        else:
            continue

        kelly = _kelly_fraction(p_win, price)
        f_stake = min(
            kelly * cfg.kelly_fraction * throttle,
            cfg.per_contract_max_pct,
        )
        if f_stake <= 0:
            continue

        notional = f_stake * bankroll_usd
        n = int(notional / max(price, 0.01))
        if n <= 0:
            continue

        targets.append(TargetPosition(
            ticker=c.ticker, side=side, target_contracts=n,
            limit_price_cents=int(round(price * 100)),
            bucket_spec=c.bucket_spec,
            rationale={
                "strategy":     "tail_probability",
                "tail":         "low" if str(c.bucket_spec).startswith("<") else "high",
                "p_model":      float(p_model),
                "p_market":     float(c.yes_ask),
                "kelly_f":      float(kelly),
                "scaled_kelly": float(f_stake),
                "exposure_frac": float(n * price / max(bankroll_usd, 1e-9)),
                "net_ev_cents": float(ev),
                "throttle":     float(throttle),
            },
        ))
        deployed_usd += n * price

    # Scale down if total exposure exceeds cap
    cap_usd = cfg.total_exposure_max_pct * bankroll_usd
    if deployed_usd > cap_usd and deployed_usd > 0:
        scale = cap_usd / deployed_usd
        targets = [replace(t, target_contracts=max(0, int(t.target_contracts * scale)))
                   for t in targets]
        targets = [t for t in targets if t.target_contracts > 0]

    return StrategyOutput(targets=targets, diagnostics={
        "strategy":      "tail_probability",
        "n_targets":     len(targets),
        "deployed_usd":  sum(t.target_contracts * t.limit_price_cents / 100 for t in targets),
        "throttle":      throttle,
    })


def run_hrrr_bias_strategy(
    cfg: StrategyCfg,
    prediction: Prediction,
    contracts: list[KalshiContract],
    feature_row: pd.Series | None,
    bankroll_usd: float,
    *,
    min_shift_F: float = 1.5,
    market_anchor_tolerance_F: float = 1.5,
    max_modal_cost: float = 0.50,
) -> StrategyOutput:
    """Bet on the model's modal when it shifts up vs market's modal AND the market's
    modal is anchored near raw HRRR.

    Mechanism: HRRR has a documented ~-2°F cool bias at KMDW (HANDOFF §1.3).
    Model learned to correct for it. When the market's modal price clusters near
    raw HRRR (= traders/algos using HRRR without bias correction) and the model
    says "actually 2°F warmer", that's a specific, replicable edge.
    """
    from weather_alpha.pmf import bucket_prob

    if feature_row is None:
        return StrategyOutput(targets=[], diagnostics={"reason": "no feature_row"})
    hrrr_max = feature_row.get("hrrr_t2m_max_peak")
    if hrrr_max is None or pd.isna(hrrr_max):
        return StrategyOutput(targets=[], diagnostics={"reason": "HRRR NaN"})
    hrrr_max_F = float(hrrr_max)   # already in °F (model_v3 §3 applied K→F at feature-build time)

    live = [c for c in contracts if c.is_live]
    if not live:
        return StrategyOutput(targets=[], diagnostics={"reason": "no live contracts"})

    def _midpoint(spec: str) -> float:
        s = str(spec).strip()
        if s.startswith("<="): return float(s[2:]) - 0.5
        if s.startswith("<"):  return float(s[1:]) - 1.0
        if s.startswith(">="): return float(s[2:]) + 0.5
        if s.startswith(">"):  return float(s[1:]) + 1.0
        if "-" in s and not s.startswith("-"):
            lo, hi = s.split("-")
            return (float(lo) + float(hi)) / 2.0
        try: return float(s)
        except ValueError: return 0.0

    scored = sorted(
        [(c, bucket_prob(prediction.pmf.values, c.bucket_spec)) for c in live],
        key=lambda x: -x[1],
    )
    model_modal_c, model_modal_p = scored[0]
    market_modal_c = max(live, key=lambda c: c.yes_ask)

    model_mid = _midpoint(model_modal_c.bucket_spec)
    market_mid = _midpoint(market_modal_c.bucket_spec)
    shift = model_mid - market_mid
    market_near_hrrr = abs(market_mid - hrrr_max_F) < market_anchor_tolerance_F

    diag_base = {
        "strategy":         "hrrr_bias",
        "hrrr_max_F":       hrrr_max_F,
        "model_modal_mid":  model_mid,
        "market_modal_mid": market_mid,
        "shift_F":          shift,
        "market_near_hrrr": bool(market_near_hrrr),
        "model_modal_p":    model_modal_p,
        "model_modal_ask":  model_modal_c.yes_ask,
    }

    if shift < min_shift_F:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"shift {shift:.1f}°F < {min_shift_F}"})
    if not market_near_hrrr:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": "market modal not anchored to HRRR"})
    if model_modal_c.yes_ask >= max_modal_cost:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"model modal ask {model_modal_c.yes_ask:.2f} >= {max_modal_cost}"})

    throttle = _regime_throttle(cfg.regime_throttle, prediction, feature_row)
    net_ev = net_ev_cents(model_modal_p, model_modal_c.yes_ask, contracts=1)
    if net_ev < cfg.edge_floor_cents:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": f"net_ev {net_ev:.1f}¢ below floor"})

    kelly = _kelly_fraction(model_modal_p, model_modal_c.yes_ask)
    f_stake = min(
        kelly * cfg.kelly_fraction * throttle,
        cfg.per_contract_max_pct,
        cfg.total_exposure_max_pct,
    )
    notional = f_stake * bankroll_usd
    n = int(notional / max(model_modal_c.yes_ask, 0.01))
    if n <= 0:
        return StrategyOutput(targets=[], diagnostics={
            **diag_base, "reason": "size below 1 contract"})

    target = TargetPosition(
        ticker=model_modal_c.ticker, side="yes", target_contracts=n,
        limit_price_cents=int(round(model_modal_c.yes_ask * 100)),
        bucket_spec=model_modal_c.bucket_spec,
        rationale={
            **diag_base,
            "kelly_f":       float(kelly),
            "scaled_kelly":  float(f_stake),
            "exposure_frac": float(n * model_modal_c.yes_ask / max(bankroll_usd, 1e-9)),
            "net_ev_cents":  float(net_ev),
            "p_model":       float(model_modal_p),
            "p_market":      float(model_modal_c.yes_ask),
            "throttle":      float(throttle),
        },
    )
    return StrategyOutput(targets=[target], diagnostics={**diag_base, "throttle": throttle})


def run_regime_confident_strategy(
    cfg: StrategyCfg,
    prediction: Prediction,
    contracts: list[KalshiContract],
    feature_row: pd.Series | None,
    bankroll_usd: float,
    *,
    peak_p_floor: float = 0.50,
    max_hrrr_gap_F: float = 4.0,
    min_margin: float = 0.05,
    fire_mode: str = "ev_gate",
    base_rate: float = 0.739,
    smooth_sigma: float = 0.0,
    force_adjacency: bool = False,
    p_model_override: dict[str, float] | None = None,
) -> StrategyOutput:
    """Two-bucket arb gated by HARD regime filter (model must be in-distribution).

    Inverts the existing throttle logic: instead of halving sizing on noisy days,
    this skips them entirely. On the days that pass the regime gate, it bypasses
    the soft throttle (uses full configured kelly_fraction).

    Hypothesis: the model's edge over the market is concentrated on regime-clean
    days. Skipping the noisy ones removes negative-edge trades without sacrificing
    edge on the good days.
    """
    if prediction.peak_P < peak_p_floor:
        return StrategyOutput(targets=[], diagnostics={
            "strategy": "regime_confident",
            "reason":  f"peak_P {prediction.peak_P:.3f} < {peak_p_floor} (model not confident)",
        })

    if feature_row is not None:
        hrrr_max = feature_row.get("hrrr_t2m_max_peak")
        cli_yest = feature_row.get("cli_high_yesterday")
        if hrrr_max is not None and not pd.isna(hrrr_max) \
                and cli_yest is not None and not pd.isna(cli_yest):
            gap = abs(float(hrrr_max) - float(cli_yest))   # both already °F
            if gap > max_hrrr_gap_F:
                return StrategyOutput(targets=[], diagnostics={
                    "strategy": "regime_confident",
                    "reason":  f"HRRR-persistence gap {gap:.1f}°F > {max_hrrr_gap_F}",
                    "gap_F":   float(gap),
                })

    # Regime clean — run two_bucket_arb but bypass throttle by temporarily zeroing
    # the two throttle conditions so _regime_throttle returns 1.0.
    out = run_two_bucket_arbitrage(
        cfg, prediction, contracts, feature_row, bankroll_usd,
        min_margin=min_margin, fire_mode=fire_mode, base_rate=base_rate,
        smooth_sigma=smooth_sigma, force_adjacency=force_adjacency,
        p_model_override=p_model_override,
    )
    out.diagnostics["strategy"] = "regime_confident"
    out.diagnostics["regime_gate"] = "passed"
    return out


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
                # hrrr_t2m_max_peak is already in °F (converted in model_v3.ipynb §3 via _K_to_F).
                gap = abs(float(hrrr_max) - float(cli_yest))
                if gap > throttle_cfg.hrrr_persistence_gap_max_f:
                    multiplier *= 0.5
                    logger.info(
                        "regime throttle: large HRRR-persistence gap (|%.1f - %.1f| = %.1f > %.1f)",
                        float(hrrr_max), float(cli_yest), gap, throttle_cfg.hrrr_persistence_gap_max_f,
                    )
        except Exception as e:
            logger.warning("regime throttle: feature lookup failed (%s)", e)

    return multiplier
