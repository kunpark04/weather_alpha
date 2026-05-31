"""Engine — one self-contained anchor cycle.

`run_cycle` is the single function that ties everything together. The scheduler invokes
it; the TUI shows its results. It is Textual-free and can be called from a one-shot CLI
or a test harness without modification.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from weather_alpha.config import Config
from weather_alpha.data import DataBundle, latest_viable_anchor, load_bundle, refresh_live
from weather_alpha.execution import ExecutionResult, KillSwitchTripped, execute, reconcile_settlements
from weather_alpha.features import build_features
from weather_alpha.kalshi import KalshiClient
from weather_alpha.live_fetchers import fetch_live_cli
from weather_alpha.model import ModelArtifacts, Prediction, predict_for_anchor
from weather_alpha.pmf import INTEGER_F_GRID
from weather_alpha.positions import Book
from weather_alpha.strategy import StrategyOutput, run_strategy, run_wing_strategy

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

    Model-free strategies use no weather inputs, so this is a no-op when model.enabled
    is false: the only data a model-free run needs is the CLI high, pulled on demand at
    settlement (see settle_if_due). Never imports herbie in this mode.
    """
    if not cfg.model.enabled:
        logger.debug("model-free: skipping weather refresh (CLI is pulled on demand at settlement)")
        return {}
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
    art: ModelArtifacts | None,
    book: Book,
    kalshi: KalshiClient,
    *,
    force_anchor: pd.Timestamp | None = None,
    skip_refresh: bool = False,
) -> CycleResult:
    """Run one anchor cycle: predict → strategize → execute → settle reconcile."""
    if cfg.model.enabled:
        if art is None:
            raise RuntimeError("model.enabled=true but no model artifacts were loaded — check model_dir")
        if not skip_refresh:
            await refresh_data(cfg, anchor_dt_local=force_anchor)
        bundle = load_bundle(cfg.paths.data_dir, cfg.station)
        anchor = force_anchor.normalize() if force_anchor is not None else \
                 latest_viable_anchor(bundle, art.t_hour_local, cfg.local_tz)
        fb = build_features(bundle, art, anchor, cfg.paths.notebook_v3)
        pred = predict_for_anchor(art, anchor, fb.feature_df)
        feature_row = fb.feature_df.loc[fb.feature_df["date"] == anchor].iloc[0] \
                      if (fb.feature_df["date"] == anchor).any() else None
        logger.info("cycle: anchor=%s mode=%s  median=%d°F  80%% CI=[%d,%d]°F  peak=%d°F (P=%.3f)",
                    anchor.date(), cfg.mode, pred.median, pred.lo10, pred.hi90, pred.peak_F, pred.peak_P)
    else:
        # Model-free path: a market-anchored strategy (market_wing) uses neither the model
        # nor weather inputs, so we load NO data bundle and fetch no weather. The anchor is
        # simply today's event date; settlement pulls the CLI high on demand (settle_if_due)
        # only when a prior-day position is still open.
        local_today = pd.Timestamp.now(tz=cfg.local_tz).normalize().tz_localize(None)
        anchor = force_anchor.normalize() if force_anchor is not None else local_today
        pred = _placeholder_prediction(cfg, anchor)
        feature_row = None
        logger.info("cycle (model-free): anchor=%s mode=%s", anchor.date(), cfg.mode)

    contracts = await kalshi.fetch_event(anchor, cfg.execution.market_event_pattern)
    live, skip_reason = _tradeable_contracts(contracts, anchor)

    bankroll = _current_bankroll(cfg, book)
    if skip_reason is not None:
        # W3: refuse to open into a closed / not-yet-open / mismatched event, and say so
        # explicitly rather than emitting nothing silently. Settlement still reconciles below.
        logger.warning("event guard: %s (anchor=%s) — no new positions this cycle",
                       skip_reason, anchor.date())
        strat = StrategyOutput(targets=[], diagnostics={"event_guard": skip_reason})
    else:
        strat = _dispatch_strategy(cfg, pred, contracts, feature_row, bankroll)

    try:
        exec_result = await execute(cfg, pred, contracts, strat, book, kalshi if cfg.is_live() else None)
    except KillSwitchTripped as e:
        # #5: the kill switch / daily-loss cap blocks NEW ORDERS only — it must NOT block
        # settlement of prior-day positions or the post-settlement bankroll re-sync (neither
        # places an order). execute() raises before placing anything, so record a no-trade
        # result and fall through to the settlement reconcile below.
        logger.warning("execution gated (%s) — placing no new orders; settlement still runs", e)
        exec_result = ExecutionResult(fills=0, skipped=0, realized_orders=[],
                                      diagnostics={"gated": str(e)})

    # Settlement: model-enabled reconciles against the freshly-refreshed bundle CLI;
    # model-free pulls the CLI high on demand only when a prior-day position is pending.
    if cfg.model.enabled:
        realized = reconcile_settlements(book, bundle.cli)
    else:
        realized = await settle_if_due(cfg, book)
    if realized:
        logger.info("settled positions realized %+d¢ this cycle", realized)
        await _resync_bankroll_after_settlement(cfg, book, kalshi)
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


def _placeholder_prediction(cfg: Config, anchor: pd.Timestamp) -> Prediction:
    """No-information uniform PMF for the model-free path. market_wing ignores it for
    decisions (anchor=market, p_used=assumed_win_prob); only diagnostics touch pred.pmf.
    Built from the canonical integer-°F grid so the model-free path loads NO artifacts."""
    grid = INTEGER_F_GRID
    p = np.full(len(grid), 1.0 / len(grid))
    return Prediction(
        date=anchor, model=f"{cfg.model.name}/model-free",
        pmf=pd.Series(p, index=grid, name="P"),
        median=int(grid[len(grid) // 2]),
        lo10=int(grid[len(grid) // 10]), hi90=int(grid[9 * len(grid) // 10]),
        peak_F=int(grid[len(grid) // 2]), peak_P=float(p[0]),
        season="NA", hard_floor=None, leaked_below_floor=0.0, nan_features=[],
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


def _tradeable_contracts(contracts: list, anchor: pd.Timestamp) -> tuple[list, str | None]:
    """W3 event-selection guard. Returns (live_contracts, skip_reason).

    A non-None skip_reason means do NOT open new positions this cycle — surfaced
    explicitly instead of silently emitting nothing. Cases: no contracts returned;
    no live contracts (event closed or not yet open); the resolved event's market
    close is already past (stale fetch / clock skew); or the resolved event ticker
    doesn't match the anchor date (wrong event — never trade it).
    """
    if not contracts:
        return [], f"no contracts returned for the {anchor.date()} event"
    live = [c for c in contracts if c.is_live]
    if not live:
        return [], "no live contracts (event closed or not yet open)"
    expected_code = anchor.strftime("%y%b%d").upper()           # e.g. 26MAY30, as built by fetch_event
    if not any(expected_code in (c.event_ticker or "") for c in live):
        return [], (f"resolved event {{{','.join(sorted({c.event_ticker for c in live}))}}} "
                    f"does not match anchor date {expected_code}")
    now_utc = pd.Timestamp.now(tz="UTC")
    closes = [c.close_time_utc for c in live if c.close_time_utc is not None]
    if closes and max(closes) <= now_utc:
        return [], f"event market already closed (latest close {max(closes)})"
    return live, None


def _current_bankroll(cfg: Config, book: Book) -> float:
    """Effective bankroll for sizing + exposure caps.

    LIVE: the real Kalshi cash balance, verified at activation and re-synced after
    each settlement (`book.bankroll_cents`; see `verify_bankroll`). The configured
    `bankroll_usd` is only a hint in LIVE.
    PAPER: starting bankroll + realized PnL − fees paid (no exchange to query).
    """
    if cfg.is_live() and book.bankroll_cents is not None:
        return max(0.0, book.bankroll_cents / 100.0)
    realized = book.realized_pnl_cents - book.fees_paid_cents
    return max(0.0, cfg.strategy.bankroll_usd + realized / 100.0)


async def verify_bankroll(cfg: Config, book: Book, client: KalshiClient) -> None:
    """Verify the running bankroll from the exchange when LIVE is activated.

    Reads Kalshi's real cash balance and adopts it as the authoritative bankroll
    (`book.bankroll_cents`) for all sizing/exposure caps — the configured
    `bankroll_usd` is only a hint. A zero balance is surfaced loudly and yields
    bankroll 0, so sizing emits no trades (a soft funding gate that never crashes
    startup). PAPER is a no-op: `_current_bankroll` computes it from realized PnL.
    """
    if not cfg.is_live():
        return
    cfg_hint_cents = int(round(cfg.strategy.bankroll_usd * 100))
    bal = await client.get_balance()
    prev = book.bankroll_cents
    book.bankroll_cents = max(0, bal)
    book.save(cfg.paths.positions_snapshot)
    logger.info("LIVE bankroll verified from Kalshi: $%.2f (config hint $%.2f%s)",
                bal / 100.0, cfg_hint_cents / 100.0,
                "" if prev is None else f", prev tracked ${prev / 100:.2f}")
    if bal <= 0:
        logger.critical("LIVE balance is $%.2f — sizing will emit NO trades until the "
                        "account is funded.", bal / 100.0)
    elif cfg_hint_cents and abs(bal - cfg_hint_cents) > cfg_hint_cents:
        logger.warning("LIVE balance $%.2f differs sharply from config bankroll $%.2f — "
                       "confirm the correct account/credentials.",
                       bal / 100.0, cfg_hint_cents / 100.0)


async def _resync_bankroll_after_settlement(cfg: Config, book: Book, client: KalshiClient) -> None:
    """Update the running bankroll after settlements realize PnL. LIVE re-queries the
    real Kalshi balance (authoritative); PAPER needs nothing — `_current_bankroll`
    already reflects the realized PnL. A balance-fetch failure never aborts the cycle."""
    if not cfg.is_live():
        return
    try:
        bal = await client.get_balance()
        book.bankroll_cents = max(0, bal)
        logger.info("LIVE bankroll re-synced after settlement: $%.2f", bal / 100.0)
    except Exception:
        logger.exception("post-settlement balance refresh failed; keeping prior bankroll")


async def settle_if_due(cfg: Config, book: Book) -> int:
    """Model-free settlement. Fetch the CLI daily-high table on demand ONLY when a
    prior-day position is still open (anchor_date < today, local) — then reconcile.
    Returns newly realized cents. No weather pull on days with nothing to settle; if the
    CLI truth hasn't landed yet it settles nothing and retries next cycle. This is the
    *only* data a model-free run fetches beyond the Kalshi market itself."""
    today = pd.Timestamp.now(tz=cfg.local_tz).normalize().date()
    has_pending = any(not p.settled and pd.Timestamp(p.anchor_date).date() < today
                      for p in book.positions.values())
    if not has_pending:
        return 0
    cli_df = await asyncio.to_thread(fetch_live_cli, cfg.station, days_back=cfg.data.cli_days_back)
    return reconcile_settlements(book, cli_df)


async def preflight(cfg: Config, book: Book, client: KalshiClient) -> None:
    """Read-only activation snapshot — wallet, open exchange positions, today's market, and
    the active strategy. Logs a summary + gentle warnings; never places or modifies anything,
    and does NOT reconcile the Book to the exchange (that is a separate, dedicated step)."""
    await verify_bankroll(cfg, book, client)
    s = cfg.strategy
    logger.info("preflight: strategy=%s model_enabled=%s flat_usd=%s max_ask_sum=%s bankroll=$%.2f",
                s.name, cfg.model.enabled, s.wing.flat_usd, s.wing.max_ask_sum, _current_bankroll(cfg, book))
    if cfg.is_live():
        try:
            positions = await client.get_positions()
            logger.info("preflight: %d open exchange position(s)", len(positions))
            for p in positions:
                held = book.open_for(p.ticker, p.side)
                book_qty = held.contracts if (held and not held.settled) else 0
                flag = "" if book_qty == p.contracts else f"  [local Book={book_qty}; reconcile pending]"
                logger.info("  %s %s x%d @ %d¢%s", p.ticker, p.side, p.contracts, p.avg_price_cents, flag)
        except Exception:
            logger.exception("preflight: get_positions failed (continuing)")
    try:
        today = pd.Timestamp.now(tz=cfg.local_tz).normalize().tz_localize(None)
        contracts = await client.fetch_event(today, cfg.execution.market_event_pattern)
        live = [c for c in contracts if c.is_live]
        logger.info("preflight: %s event — %d contracts (%d live)", today.date(), len(contracts), len(live))
        if not live:
            logger.warning("preflight: no live contracts for today's event — engine will no-trade until it opens")
    except Exception:
        logger.exception("preflight: market fetch failed (continuing)")
