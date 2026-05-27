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
from forecast_alpha.data import DataBundle, latest_viable_anchor, load_bundle, refresh_hrrr, refresh_iem
from forecast_alpha.execution import ExecutionResult, execute, reconcile_settlements
from forecast_alpha.features import build_features
from forecast_alpha.kalshi import KalshiClient
from forecast_alpha.model import ModelArtifacts, Prediction, predict_for_anchor
from forecast_alpha.positions import Book
from forecast_alpha.strategy import StrategyOutput, run_strategy

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


def refresh_data(cfg: Config) -> None:
    """Run both data-refresh subprocesses. Slow (~30–60s). Call from scheduler at cadence."""
    project_root = Path(cfg.paths.data_dir).parent
    refresh_iem(project_root)
    refresh_hrrr(project_root, cfg.local_tz)


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
        refresh_data(cfg)

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
    strat = run_strategy(cfg.strategy, pred, contracts, feature_row, bankroll)

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


def _current_bankroll(cfg: Config, book: Book) -> float:
    """Effective bankroll = starting bankroll + realized PnL − fees paid (paper).

    In LIVE mode we'd query Kalshi's /portfolio/balance; for v1 we keep symmetry
    with paper math so behavior is identical across modes.
    """
    realized = book.realized_pnl_cents - book.fees_paid_cents
    return max(0.0, cfg.strategy.bankroll_usd + realized / 100.0)
