"""Engine — one self-contained anchor cycle.

`run_cycle` is the single function that ties everything together. The scheduler invokes
it; the TUI shows its results. It is Textual-free and can be called from a one-shot CLI
or a test harness without modification.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from forecast_alpha.config import Config
from forecast_alpha.data import DataBundle, latest_viable_anchor, load_bundle, refresh_live
from forecast_alpha.execution import ExecutionResult, execute, reconcile_settlements
from forecast_alpha.features import build_features
from forecast_alpha.kalshi import KalshiClient
from forecast_alpha.model import ModelArtifacts, Prediction, predict_for_anchor
from forecast_alpha.positions import Book
from forecast_alpha.strategy import StrategyOutput, run_strategy, run_wing_strategy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CycleResult:
    anchor_date: pd.Timestamp
    prediction: Prediction
    contracts_count: int
    live_contracts: int
    strategy: StrategyOutput
    execution: ExecutionResult
    realized_at_settle: int
    diagnostics: dict = field(default_factory=dict)


async def refresh_data(cfg: Config, anchor_dt_local: pd.Timestamp | None = None) -> dict[str, int]:
    """In-process parallel fetch of all 5 weather sources → merge into parquets.

    Replaces the old subprocess wrappers. Wall time ~3–5 s vs ~30–90 s before.
    Anchor defaults to "now" — HRRR auto-picks the latest safely-published init.
    """
    if anchor_dt_local is None:
        anchor_dt_local = pd.Timestamp.now(tz=cfg.local_tz)
    return await refresh_live(
        station=cfg.station,
        anchor_dt_local=anchor_dt_local,
        data_dir=cfg.paths.data_dir,
        local_tz=cfg.local_tz,
        asos_source=cfg.resolved_asos_source(),
        synoptic_token=cfg.synoptic_token(),
    )


async def run_cycle(
    cfg: Config,
    art: ModelArtifacts,
    book: Book,
    kalshi: KalshiClient,
    *,
    force_anchor: pd.Timestamp | None = None,
    skip_refresh: bool = False,
) -> CycleResult:
    """Run one anchor cycle: predict → strategize → execute → settle reconcile."""
    if not skip_refresh:
        await refresh_data(cfg, anchor_dt_local=force_anchor)

    bundle = load_bundle(cfg.paths.data_dir, cfg.station)

    anchor = force_anchor.normalize() if force_anchor is not None else \
             latest_viable_anchor(bundle, art.t_hour_local, cfg.local_tz)
    logger.info("cycle: anchor=%s mode=%s", anchor.date(), cfg.mode)

    fb = build_features(bundle, art, anchor, cfg.paths.notebook_v3)
    pred = predict_for_anchor(art, anchor, fb.feature_df)
    logger.info("predicted %s: median=%d°F  80%% CI=[%d,%d]°F  peak=%d°F (P=%.3f)",
                anchor.date(), pred.median, pred.lo10, pred.hi90, pred.peak_F, pred.peak_P)

    contracts = await kalshi.fetch_event(anchor, cfg.execution.market_event_pattern)
    live = [c for c in contracts if c.is_live]

    feature_row = fb.feature_df.loc[fb.feature_df["date"] == anchor].iloc[0] \
                  if (fb.feature_df["date"] == anchor).any() else None

    bankroll = _current_bankroll(cfg, book)
    strat = _dispatch_strategy(cfg, pred, contracts, feature_row, bankroll)

    exec_result = await execute(cfg, pred, contracts, strat, book, kalshi if cfg.is_live() else None)

    realized = reconcile_settlements(book, bundle.cli)
    if realized:
        logger.info("settled positions realized %+d¢ this cycle", realized)
        book.save(cfg.paths.positions_snapshot)

    return CycleResult(
        anchor_date=anchor,
        prediction=pred,
        contracts_count=len(contracts),
        live_contracts=len(live),
        strategy=strat,
        execution=exec_result,
        realized_at_settle=realized,
        diagnostics={
            "bankroll_usd": bankroll,
            "open_exposure_cents": book.exposure_cents(),
            "realized_pnl_cents": book.realized_pnl_cents,
        },
    )


def _dispatch_strategy(cfg: Config, pred, contracts, feature_row, bankroll: float) -> StrategyOutput:
    """Route to the configured strategy. Wing variants pull their knobs from cfg.strategy.wing."""
    if cfg.strategy.name in ("wing", "wing_any", "market_wing"):
        w = cfg.strategy.wing
        return run_wing_strategy(
            cfg.strategy, pred, contracts, feature_row, bankroll,
            wing_anchor=w.anchor,
            require_agreement=w.require_agreement,
            drop_lower_ask=w.drop_lower_ask,
            assumed_win_prob=w.assumed_win_prob,
            max_ask_sum=w.max_ask_sum,
            sizing_mode=w.sizing_mode,
            flat_stake_usd=(w.flat_usd if w.flat_usd > 0 else None),
            fee_aware=w.fee_aware,
        )
    return run_strategy(cfg.strategy, pred, contracts, feature_row, bankroll)


def _current_bankroll(cfg: Config, book: Book) -> float:
    """Effective bankroll = starting bankroll + realized PnL − fees paid (paper).

    In LIVE mode we'd query Kalshi's /portfolio/balance; for v1 we keep symmetry
    with paper math so behavior is identical across modes.
    """
    realized = book.realized_pnl_cents - book.fees_paid_cents
    return max(0.0, cfg.strategy.bankroll_usd + realized / 100.0)
