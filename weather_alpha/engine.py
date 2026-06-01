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

from weather_alpha.config import Config, MarketCfg
from weather_alpha.data import DataBundle, latest_viable_anchor, load_bundle, refresh_live
from weather_alpha.execution import ExecutionResult, KillSwitchTripped, execute, reconcile_settlements
from weather_alpha.features import build_features
from weather_alpha.kalshi import KalshiClient, event_date_code
from weather_alpha.live_fetchers import fetch_live_cli
from weather_alpha.model import ModelArtifacts, Prediction, predict_for_anchor
from weather_alpha.pmf import INTEGER_F_GRID
from weather_alpha.positions import Book
from weather_alpha.report import report, usd
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
    markets: list[MarketCfg] | None = None,
) -> list[CycleResult]:
    """Run one anchor cycle for the given markets (default cfg.markets), sharing Book + bankroll.

    Returns one CycleResult per market (decision + execution + that market's per-station
    settlement). Live/paper is a process-level property, so all markets here share the same
    mode and the same real account. The model-enabled path is single-station (v3 artifacts
    are KMDW-specific), so it requires exactly one market; multi-market is the model-free path.
    """
    bundle: DataBundle | None = None
    if cfg.model.enabled:
        if art is None:
            raise RuntimeError("model.enabled=true but no model artifacts were loaded — check model_dir")
        if len(cfg.markets) != 1:
            raise RuntimeError(
                "model.enabled=true supports a single market (v3 artifacts are station-specific); "
                "use a model-free strategy (market_wing) for multi-market."
            )
        if not skip_refresh:
            await refresh_data(cfg, anchor_dt_local=force_anchor)
        bundle = load_bundle(cfg.paths.data_dir, cfg.station)

    target_markets = markets if markets is not None else cfg.markets
    # W4: latch any pre-existing drawdown BEFORE trading, so the first city is gated too.
    _latch_halts(cfg, book)

    results: list[CycleResult] = []
    total_realized = 0
    for market in target_markets:
        try:
            r = await _run_one_market(cfg, market, art, book, kalshi,
                                      bundle=bundle, force_anchor=force_anchor)
        except Exception:
            # Isolate a per-market failure (bad data / parse / network) so one city can
            # never take down the others — critical when a live city shares the process.
            logger.exception("[%s] cycle failed — isolated; other markets continue", market.name)
            report(f"[{market.name}] ❌ ERROR — cycle failed (see logs); other cities unaffected")
            continue
        results.append(r)
        total_realized += r.realized_at_settle
        # W4: re-latch after THIS market's settlement so an account-level stop it trips halts
        # the REMAINING cities this same cycle (each market checks is_halted before deciding).
        _latch_halts(cfg, book)

    # Post-settlement bankroll re-sync (LIVE only; PAPER no-op), then a final re-latch against
    # the resynced balance. I7: net-zero realized => settlements offset => cash balance unchanged,
    # so skipping the re-sync is correct (it would re-fetch the same balance); self-heals next time.
    if total_realized:
        await _resync_bankroll_after_settlement(cfg, book, kalshi)
        _latch_halts(cfg, book)
    book.save(cfg.paths.positions_snapshot)
    return results


async def _run_one_market(
    cfg: Config,
    market: MarketCfg,
    art: ModelArtifacts | None,
    book: Book,
    kalshi: KalshiClient,
    *,
    bundle: DataBundle | None,
    force_anchor: pd.Timestamp | None = None,
) -> CycleResult:
    """One city's slice of a cycle: predict → strategize → execute → settle (its station)."""
    if cfg.model.enabled:
        assert bundle is not None and art is not None
        anchor = force_anchor.normalize() if force_anchor is not None else \
                 latest_viable_anchor(bundle, art.t_hour_local, market.local_tz)
        fb = build_features(bundle, art, anchor, cfg.paths.notebook_v3)
        pred = predict_for_anchor(art, anchor, fb.feature_df)
        feature_row = fb.feature_df.loc[fb.feature_df["date"] == anchor].iloc[0] \
                      if (fb.feature_df["date"] == anchor).any() else None
        logger.info("cycle [%s]: anchor=%s mode=%s  median=%d°F  80%% CI=[%d,%d]°F  peak=%d°F (P=%.3f)",
                    market.name, anchor.date(), cfg.mode, pred.median, pred.lo10, pred.hi90,
                    pred.peak_F, pred.peak_P)
    else:
        # Model-free path: market_wing uses neither the model nor weather, so we load NO
        # bundle and fetch no weather. Anchor = today's event date in the MARKET's tz;
        # settlement pulls the CLI high on demand (settle_market_if_due) only when a
        # prior-day position for this station is still open.
        local_today = pd.Timestamp.now(tz=market.local_tz).normalize().tz_localize(None)
        anchor = force_anchor.normalize() if force_anchor is not None else local_today
        pred = _placeholder_prediction(cfg, anchor)
        feature_row = None
        logger.info("cycle [%s] (model-free): anchor=%s mode=%s", market.name, anchor.date(), cfg.mode)

    contracts = await kalshi.fetch_event(anchor, market.event_pattern)
    live, skip_reason = _tradeable_contracts(contracts, anchor)

    bankroll = _current_bankroll(cfg, book)
    tag = f"[{pd.Timestamp.now(tz=market.local_tz):%H:%M} {market.name}]"
    if book.is_halted(market.station):
        pct = cfg.risk.account_drawdown_pct if book.account_halted else cfg.risk.per_city_drawdown_pct
        scope = "account" if book.account_halted else "city"
        reason = f"{scope} drawdown halt (>= {pct:.0%} of running account)"
        logger.warning("[%s] HALTED — %s; no new trades until reset", market.name, reason)
        report(f"{tag} ⛔ HALTED — {reason}; reset: "
               f"python scripts/halt.py --reset {market.station} --config {cfg.config_path}")
        strat = StrategyOutput(targets=[], diagnostics={"halted": reason})
    elif skip_reason is not None:
        # W3: refuse to open into a closed / not-yet-open / mismatched event, said explicitly
        # rather than emitting nothing silently. Settlement still reconciles below.
        logger.warning("[%s] event guard: %s (anchor=%s) — no new positions this cycle",
                       market.name, skip_reason, anchor.date())
        report(f"{tag} ⏭️  SKIP — {skip_reason}")
        strat = StrategyOutput(targets=[], diagnostics={"event_guard": skip_reason})
    else:
        strat = _dispatch_strategy(cfg, pred, contracts, feature_row, bankroll)
        _report_decision(tag, strat)

    try:
        exec_result = await execute(cfg, pred, contracts, strat, book,
                                    kalshi if cfg.is_live() else None, market=market,
                                    bankroll_usd=bankroll)
    except KillSwitchTripped as e:
        # #5: the kill switch / daily-loss cap blocks NEW ORDERS only — settlement of
        # prior-day positions (below) and the bankroll re-sync must still run.
        logger.warning("[%s] execution gated (%s) — no new orders; settlement still runs",
                       market.name, e)
        report(f"{tag} ⛔ GATED — {e}; no new orders (settlement still runs)")
        exec_result = ExecutionResult(fills=0, skipped=0, realized_orders=[],
                                      diagnostics={"gated": str(e)})
    _report_fills(tag, exec_result)

    # Settlement for THIS market's station only.
    if cfg.model.enabled:
        assert bundle is not None
        realized = reconcile_settlements(book, bundle.cli, station=market.station)
    else:
        realized = await settle_market_if_due(cfg, market, book)
    if realized:
        logger.info("[%s] settled positions realized %+d¢ this cycle", market.name, realized)
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
            "market": market.name,
            "station": market.station,
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


def _report_decision(tag: str, strat: StrategyOutput) -> None:
    """One concise terminal line for the 1 PM decision: ENTER (the legs) or SKIP (reason)."""
    if not strat.targets:
        d = strat.diagnostics
        reason = d.get("skip_reason") or d.get("reason")
        if not reason:
            bits = [f"{k}={d[k]}" for k in ("sum_asks", "ev_margin", "agreement") if k in d]
            reason = "no qualifying wing" + (f" ({', '.join(bits)})" if bits else "")
        report(f"{tag} ⏭️  SKIP — {reason}")
        return
    legs = ", ".join(f"{t.side.upper()} {t.bucket_spec} @ {t.limit_price_cents}¢" for t in strat.targets)
    stake_c = sum(t.target_contracts * t.limit_price_cents for t in strat.targets)
    sa = strat.diagnostics.get("sum_asks")
    sa_txt = f"sum {sa:.2f}, " if isinstance(sa, (int, float)) else ""
    report(f"{tag} ✅ ENTER — {legs}  ({sa_txt}stake {usd(stake_c)})")


def _report_fills(tag: str, exec_result: ExecutionResult) -> None:
    """One concise terminal line summarizing what actually filled this cycle."""
    if exec_result.fills <= 0:
        return
    orders = exec_result.realized_orders
    outlay = sum(o.get("contracts", 0) * o.get("fill_price_cents", 0) for o in orders)
    fees = sum(o.get("fee_cents", 0) for o in orders)
    report(f"{tag} 💸 FILLED {exec_result.fills} leg(s) — outlay {usd(outlay)}, fee {usd(fees)}")


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
    expected_code = event_date_code(anchor)                     # e.g. 26MAY30, as built by fetch_event (I1: locale-safe)
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
    PAPER: starting bankroll + realized PnL (already net of fees via settle; no exchange to query).
    """
    if cfg.is_live() and book.bankroll_cents is not None:
        return max(0.0, book.bankroll_cents / 100.0)
    # #5: realized_pnl_cents is ALREADY net of fees (Book.settle subtracts total_fees_cents); do
    # NOT subtract fees_paid_cents again — that double-counted settled fees and understated the
    # paper bankroll (skewing sizing / exposure / drawdown thresholds).
    return max(0.0, cfg.strategy.bankroll_usd + book.realized_pnl_cents / 100.0)


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


def _latch_halts(cfg: Config, book: Book) -> None:
    """Refresh drawdown peaks + latch per-city/account halts against the running balance,
    reporting any newly-latched halt. Called before the market loop and after each market's
    settlement so an account-level stop halts the remaining cities in the same cycle (W4)."""
    # I5: both the threshold base (balance) and the drawdown numerator (realized, via
    # update_drawdown_halts) are ~net of fees, so the convention is consistent to second order.
    balance_cents = (book.bankroll_cents if (cfg.is_live() and book.bankroll_cents is not None)
                     else int(round(_current_bankroll(cfg, book) * 100)))
    for label in book.update_drawdown_halts(balance_cents, cfg.risk.per_city_drawdown_pct,
                                            cfg.risk.account_drawdown_pct):
        logger.critical("DRAWDOWN HALT latched: %s", label)
        report(f"⛔ HALT LATCHED — {label}; stays halted until you reset it "
               f"(python scripts/halt.py --reset --config {cfg.config_path})")


async def settle_market_if_due(cfg: Config, market: MarketCfg, book: Book) -> int:
    """Model-free per-market settlement. Fetch the CLI daily-high for THIS market's station
    on demand ONLY when one of its prior-day positions is still open (anchor_date < today in
    the market's tz) — then reconcile just that station's positions. Returns newly realized
    cents. No weather pull on days with nothing to settle; if the CLI truth hasn't landed yet
    it settles nothing and retries next cycle. This is the *only* data a model-free run
    fetches beyond the Kalshi market itself."""
    today = pd.Timestamp.now(tz=market.local_tz).normalize().date()
    has_pending = any(not p.settled and p.station == market.station
                      and pd.Timestamp(p.anchor_date).date() < today
                      for p in book.positions.values())
    if not has_pending:
        return 0
    cli_df = await asyncio.to_thread(fetch_live_cli, market.station, days_back=cfg.data.cli_days_back)
    return reconcile_settlements(book, cli_df, station=market.station)


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
    for market in cfg.markets:
        try:
            today = pd.Timestamp.now(tz=market.local_tz).normalize().tz_localize(None)
            contracts = await client.fetch_event(today, market.event_pattern)
            live = [c for c in contracts if c.is_live]
            logger.info("preflight [%s]: %s event — %d contracts (%d live)",
                        market.name, today.date(), len(contracts), len(live))
            if not live:
                logger.warning("preflight [%s]: no live contracts for today's event — "
                               "will no-trade until it opens", market.name)
        except Exception:
            logger.exception("preflight [%s]: market fetch failed (continuing)", market.name)
