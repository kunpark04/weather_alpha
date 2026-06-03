"""Execution engine — translates TargetPosition list into orders + fills + log rows.

PAPER mode (default): simulate fills at config.execution.fill_model (ask/mid/bid) with
optional slippage; update an in-memory Book.

LIVE mode: submit limit orders to Kalshi via KalshiClient; fills are recorded on
order acknowledgement (a follow-up step in the scheduler can reconcile open orders
against actual fills via the orders endpoint — kept simple here).

Both modes share:
  - Pre-trade kill-switch + daily-loss check
  - Fee accounting via fees.trade_fee_cents
  - One LogRow per (target ∪ live-but-no-target) contract → live_log.parquet
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

import pandas as pd

from weather_alpha.config import Config, MarketCfg
from weather_alpha.fees import trade_fee_cents
from weather_alpha.kalshi import KalshiClient, KalshiContract, order_ack_fee_cents
from weather_alpha.live_log import LogRow, append_rows
from weather_alpha.model import Prediction
from weather_alpha.positions import Book, RunBudget
from weather_alpha.report import report, usd_signed
from weather_alpha.strategy import StrategyOutput, TargetPosition

logger = logging.getLogger(__name__)

# LIVE fill-confirmation poll: a marketable limit usually fills within ~1s, but the
# portfolio endpoint can lag the matching engine. Poll a few times before concluding
# the order is resting/rejected. Conservative; only affects the LIVE path.
_FILL_POLL_TRIES = 3
_FILL_POLL_DELAY_S = 1.0


class KillSwitchTripped(RuntimeError):
    pass


@dataclass(frozen=True)
class ExecutionResult:
    fills: int
    skipped: int
    realized_orders: list[dict]
    diagnostics: dict


async def execute(
    cfg: Config,
    prediction: Prediction,
    contracts: list[KalshiContract],
    strategy: StrategyOutput,
    book: Book,
    client: KalshiClient | None,
    *,
    market: MarketCfg | None = None,
    bankroll_usd: float = 0.0,
    budget: RunBudget | None = None,
) -> ExecutionResult:
    """Run execution for one anchor cycle. Mutates `book` in place. Appends log rows.

    `market` (multi-market) supplies the settlement station tagged onto each fill and the
    local tz for the log timestamp; when None, the legacy cfg.station/cfg.local_tz are used.
    `bankroll_usd` (the running account balance) enables the process-wide exposure cap; when
    0 the cap is skipped (non-engine callers) — the daily-outlay cap still applies.
    `budget` (W1) is the shared per-CYCLE order-attempt backstop; when None a private one is
    built from cfg.risk.per_anchor_max_trades (legacy / single-call semantics).

    W2: a wing is ATOMIC w.r.t. the risk gates. All target-leg deltas are planned first, then
    the kill-switch + daily-outlay + exposure + order-budget caps are checked ONCE against the
    FULL leg set's incremental cost; only if all pass are the legs placed — without re-checking
    caps/kill between legs — so a cap/kill trip can never leave a half-placed (naked) wing. The
    kill-switch is thus evaluated at wing granularity (a wing completes in seconds), not per leg.

    I2: the account-wide realized daily-loss cap is checked ONCE per cycle in engine.run_cycle
    (`check_daily_loss`), not here; execute() still hard-aborts on the kill-switch pre-check.
    """
    _check_kill_switch(cfg)
    station = market.station if market is not None else cfg.station
    tz = market.local_tz if market is not None else cfg.local_tz
    if budget is None:
        budget = RunBudget(cap=cfg.risk.per_anchor_max_trades)
    # #3: for LIVE, size deltas against EXCHANGE truth (not the local Book), so a late fill /
    # manual trade / stale snapshot can't cause over- or under-buying. One snapshot per cycle; on
    # fetch failure fall back to the Book (live_held stays None). I3: this same snapshot is the
    # delta authority AND is handed to _execute_one as its pre-order baseline (no redundant fetch).
    live_held: dict[tuple[str, str], tuple[int, int]] | None = None
    if cfg.is_live() and client is not None:
        try:
            live_held = {(p.ticker, p.side): (p.contracts, p.avg_price_cents)
                         for p in await client.get_positions()}
        except Exception:
            logger.exception("exposure: get_positions failed; using Book for deltas this cycle")

    target_by_ticker = {t.ticker: t for t in strategy.targets}
    rows: list[LogRow] = []
    skipped = 0
    orders: list[dict] = []

    run_utc = pd.Timestamp.now(tz="UTC")
    t_utc = _t_utc_for(prediction.date, cfg, tz)
    anchor_iso = str(prediction.date.date())

    # ---- Plan phase: resolve every placeable leg (the deltas after the at-target / not-live /
    # no-target filters). Emit skip rows for filtered contracts here so they're logged regardless
    # of whether the wing is later gated. `_PlannedLeg` carries the pre-order LIVE baseline (I3).
    planned: list[_PlannedLeg] = []
    for c in contracts:
        tgt = target_by_ticker.get(c.ticker)
        if tgt is None:
            rows.append(_no_target_row(cfg, run_utc, t_utc, prediction, c))
            skipped += 1
            continue
        if not c.is_live:
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt, reason="not_live"))
            skipped += 1
            continue
        # Target-vs-current diff. Strategy emits absolute targets ("hold 646 YES"); execution
        # only fills the delta. Without this, intraday refreshes compound positions every cycle.
        if live_held is not None:
            current_qty, current_avg = live_held.get((tgt.ticker, tgt.side), (0, 0))  # #3 exchange truth
        else:
            current = book.open_for(tgt.ticker, tgt.side)
            current_qty = current.contracts if (current and not current.settled) else 0
            current_avg = 0
        delta_qty = tgt.target_contracts - current_qty
        if delta_qty <= 0:
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt, reason="at_target"))
            skipped += 1
            continue
        tgt_delta = replace(tgt, target_contracts=delta_qty)
        planned.append(_PlannedLeg(c=c, tgt=tgt_delta,
                                   delta_cost_cents=delta_qty * tgt_delta.limit_price_cents,
                                   before_qty=current_qty, before_avg=current_avg))

    # ---- Gate phase (W2): check kill + budget + outlay + exposure against the WHOLE wing ONCE.
    # If any binds, skip EVERY planned leg (place none) so the wing is never left naked.
    halt_reason = _gate_wing(cfg, book, budget, planned, anchor_iso, bankroll_usd) if planned else None
    if halt_reason is not None:
        for leg in planned:
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, leg.c, leg.tgt, reason=halt_reason))
            skipped += 1
        planned = []

    # ---- Placement phase (W2): place all gated-through legs WITHOUT re-checking caps/kill
    # between them. A mid-wing leg failure after >=1 leg has already filled triggers a flatten of
    # the filled leg(s) so the day never ends on a naked directional leg.
    fills = 0
    placed_this_wing: list[dict] = []
    leg_error = False
    for leg in planned:
        budget.attempts += 1   # #10: a submitted order consumes the budget whether or not it fills
        try:
            fill = await _execute_one(cfg, leg.c, leg.tgt, client, anchor_date=anchor_iso,
                                      run_utc=run_utc,
                                      before=(leg.before_qty, leg.before_avg) if live_held is not None else None)
        except Exception:
            logger.exception("leg %s raised in _execute_one — flattening any filled legs of this wing",
                             leg.c.ticker)
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, leg.c, leg.tgt, reason="error"))
            skipped += 1
            leg_error = True
            break
        if fill is None:
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, leg.c, leg.tgt, reason="rejected"))
            skipped += 1
            continue
        book.add_fill(
            ticker=leg.c.ticker, side=leg.tgt.side,
            contracts=fill["contracts"], fill_cents=fill["fill_price_cents"],
            fee_cents=fill["fee_cents"], bucket_spec=leg.tgt.bucket_spec,
            opened_utc=run_utc.isoformat(), anchor_date=anchor_iso,
            station=station,
        )
        fills += 1
        orders.append(fill)
        placed_this_wing.append(fill)
        rows.append(_filled_row(cfg, run_utc, t_utc, prediction, leg.c, leg.tgt, fill))

    if leg_error and placed_this_wing:
        await _flatten_legs(cfg, client, book, placed_this_wing)

    if rows:
        append_rows(cfg.paths.live_log, rows)
    book.save(cfg.paths.positions_snapshot)

    logger.info(
        "execution [%s]: fills=%d skipped=%d  realized_pnl=%d¢  fees=%d¢  open_exposure=%d¢",
        cfg.mode, fills, skipped, book.realized_pnl_cents, book.fees_paid_cents,
        book.exposure_cents(),
    )
    return ExecutionResult(
        fills=fills,
        skipped=skipped,
        realized_orders=orders,
        diagnostics={
            "mode": cfg.mode,
            "open_exposure_cents": book.exposure_cents(),
            "realized_pnl_cents": book.realized_pnl_cents,
            "halted_mid_cycle": halt_reason is not None or leg_error,
            "halt_reason": halt_reason or ("leg_error" if leg_error else None),
        },
    )


@dataclass(frozen=True)
class _PlannedLeg:
    c: KalshiContract
    tgt: TargetPosition
    delta_cost_cents: int
    before_qty: int        # I3: pre-order exchange holding for this ticker/side (LIVE)
    before_avg: int


def _gate_wing(cfg: Config, book: Book, budget: RunBudget, planned: list[_PlannedLeg],
               anchor_iso: str, bankroll_usd: float) -> str | None:
    """W2 atomic gate: evaluate the FULL planned wing against every risk gate ONCE, before any
    leg is placed. Returns the binding gate's reason (so the whole wing is skipped) or None to
    proceed. Order mirrors the old per-leg precedence: kill > budget > daily-outlay > exposure.

    I1: the incoming wing is costed at limit_price (`delta_cost_cents`) while already-open stake
    in book.exposure_cents()/daily_outlay_cents() is at realized avg_cost — within a cent or two
    of the limit for a marketable taker order, so the caps are apples-to-limit, not apples-to-avg.
    Aligning is optional and intentionally deferred."""
    wing_cost = sum(leg.delta_cost_cents for leg in planned)

    if _kill_switch_armed(cfg):
        logger.error("KILL_SWITCH armed — skipping the entire %d-leg wing (placing none)", len(planned))
        return "kill_switch"

    # W1: the whole wing must fit the SHARED per-cycle order budget (sums across all cities).
    if len(planned) > budget.remaining():
        logger.error("per_anchor_max_trades budget=%d, used=%d — %d-leg wing won't fit; skipping wing",
                     budget.cap, budget.attempts, len(planned))
        return "max_trades"

    # C3 intraday circuit breaker: worst-case same-day loss = total stake outlaid today. Check the
    # whole wing's incremental outlay against the cap (daily_loss_cents only accrues next-day).
    cap_cents = int(round(cfg.risk.daily_max_loss_usd * 100))
    outlay_cents = book.daily_outlay_cents(anchor_iso)
    if outlay_cents + wing_cost > cap_cents:
        logger.error("daily outlay cap: %d¢ + %d¢ (wing) > %d¢ ($%.2f) — skipping wing",
                     outlay_cents, wing_cost, cap_cents, cfg.risk.daily_max_loss_usd)
        return "daily_outlay_cap"

    # C2 process-wide exposure cap: cumulative OPEN stake across ALL cities must stay within
    # total_exposure_max_pct of the running balance. Skipped when bankroll_usd is unknown (0).
    if bankroll_usd > 0:
        exp_cap_cents = int(round(cfg.strategy.total_exposure_max_pct * bankroll_usd * 100))
        if book.exposure_cents() + wing_cost > exp_cap_cents:
            logger.error("exposure cap: open %d¢ + %d¢ (wing) > %d¢ (%.0f%% of $%.2f) — skipping wing",
                         book.exposure_cents(), wing_cost, exp_cap_cents,
                         cfg.strategy.total_exposure_max_pct * 100, bankroll_usd)
            return "exposure_cap"
    return None


async def _flatten_legs(cfg: Config, client: KalshiClient | None, book: Book,
                        filled: list[dict]) -> None:
    """W2 safety net: an order error AFTER >=1 leg of a wing has filled leaves a naked directional
    leg. Offset each filled leg with a same-size sell so the day doesn't end lopsided. On a
    successful flatten, drop the leg from the Book so it matches the exchange (else the saved Book
    keeps a phantom-open position that next-day settlement would mis-resolve and the delta logic
    would re-buy). PAPER has no exchange (no-op, leg left as-is). A flatten failure is logged
    CRITICAL (operator must square the book) AND the position is KEPT in the Book (it IS still
    held) — never re-raised, so the cycle's own book.save still runs."""
    if not (cfg.is_live() and client is not None):
        logger.warning("mid-wing leg error (PAPER) — %d filled leg(s) left as-is (no exchange to flatten)",
                       len(filled))
        return
    for f in filled:
        # W-A: sell the FULL booked holding (any prior-cycle fills + this one), NOT just this
        # cycle's delta `f["contracts"]`. `remove_position` below drops the WHOLE position, so a
        # partial sell would leave the remainder naked on the exchange while the Book reads flat —
        # the exact tail risk flatten exists to kill. add_fill accumulates, so book.open_for is the
        # true holding (== exchange truth, since W3 cancels any partial remainder each cycle).
        pos = book.open_for(f["ticker"], f["side"])
        qty = pos.contracts if (pos is not None and not pos.settled) else f["contracts"]
        try:
            # Marketable exit: a SELL at limit 1¢ crosses any resting bid and (by price-time
            # priority) fills at the prevailing BID, not at 1¢ — so it exits at market without
            # giving the position away. 1, not 0, because Kalshi prices are 1–99¢ (0 is rejected).
            await client.place_order(
                ticker=f["ticker"], side=f["side"], action="sell",
                count=qty, limit_price_cents=1,
                client_order_id=f"wa-flat-{f['ticker']}-{f['side']}-{int(pd.Timestamp.now(tz='UTC').timestamp() * 1000)}",
            )
            book.remove_position(f["ticker"], f["side"])   # Book now flat for this leg, like the exchange
            logger.critical("FLATTENED naked leg after mid-wing error: sold %d %s %s (removed from Book)",
                            qty, f["side"], f["ticker"])
        except Exception:
            logger.critical("FAILED to flatten naked leg %s %s x%d after mid-wing error — leg KEPT in "
                            "Book (still held); MANUAL INTERVENTION REQUIRED to square the book",
                            f["side"], f["ticker"], qty)


# ---------------------------------------------------------------------------
# Per-contract execution
# ---------------------------------------------------------------------------

async def _live_held(client: KalshiClient, ticker: str, side: str) -> tuple[int, int]:
    """True (contracts, avg_price_cents) currently held on the exchange for one
    ticker/side. Returns (0, 0) if flat. Source of truth for C1 fill confirmation —
    never trust the local Book for what actually filled."""
    for p in await client.get_positions():
        if p.ticker == ticker and p.side == side:
            return p.contracts, p.avg_price_cents
    return 0, 0


async def _execute_one(cfg: Config, c: KalshiContract, tgt: TargetPosition,
                       client: KalshiClient | None, *, anchor_date: str,
                       run_utc: pd.Timestamp,
                       before: tuple[int, int] | None = None) -> dict | None:
    fill_price_cents = _paper_fill_cents(cfg, c, tgt)
    if fill_price_cents > tgt.limit_price_cents:
        # Won't pay through our limit — same logic for paper + live.
        return None

    if cfg.is_live():
        if client is None:
            raise RuntimeError("LIVE mode requires an authenticated KalshiClient")
        # W3 — idempotency key UNIQUE PER CYCLE ATTEMPT: append the cycle's run_utc ms so a
        # remainder placed on a LATER cycle gets a fresh coid and isn't rejected as a duplicate of
        # this cycle's order. Over-buying is independently prevented by the #3 exchange-truth delta
        # (planned against get_positions every cycle), so a unique coid cannot double-buy.
        coid = f"wa-{anchor_date}-{tgt.ticker}-{tgt.side}-{int(run_utc.timestamp() * 1000)}"
        # I3: reuse the cycle's get_positions snapshot as the pre-order baseline when available;
        # only fetch fresh if the planner didn't pass one (fallback / safety).
        if before is not None:
            before_qty, before_avg = before
        else:
            before_qty, before_avg = await _live_held(client, tgt.ticker, tgt.side)
        ack = await client.place_order(
            ticker=tgt.ticker, side=tgt.side, action="buy",
            count=tgt.target_contracts, limit_price_cents=tgt.limit_price_cents,
            client_order_id=coid,
        )
        oid = ack.get("order_id") or (ack.get("order") or {}).get("order_id")
        # C1 — confirm the ACTUAL fill from the exchange rather than assuming the full
        # size filled at our limit. A resting/partial/rejected order books only what
        # truly filled; nothing filled -> return None so the next cycle retries (the
        # Book's delta logic stays correct because we never recorded a phantom fill).
        filled, price_cents = 0, tgt.limit_price_cents
        for _attempt in range(_FILL_POLL_TRIES):
            after_qty, after_avg = await _live_held(client, tgt.ticker, tgt.side)
            filled = max(0, after_qty - before_qty)
            if filled > 0:
                # #6: marginal price of THIS fill (back out the pre-order holding), not the blended
                # post-order average — else topping up an existing holding books a blended avg.
                marginal = (after_qty * after_avg - before_qty * before_avg) / filled
                price_cents = int(round(marginal)) if marginal > 0 else tgt.limit_price_cents
                break
            if _attempt + 1 < _FILL_POLL_TRIES:
                await asyncio.sleep(_FILL_POLL_DELAY_S)
        if filled <= 0:
            # #4: cancel the resting order so it can't fill LATER (after the Book has saved),
            # silently diverging Book from exchange. Best-effort: if it filled in the race the
            # cancel errors harmlessly and the #3 exchange-truth delta corrects sizing next cycle.
            await _cancel_quietly(client, oid, tgt.ticker)
            logger.warning("LIVE order %s (id=%s) no fill after %d polls; cancelled, retry next cycle",
                           tgt.ticker, coid, _FILL_POLL_TRIES)
            return None
        if filled < tgt.target_contracts:
            # W3 — PARTIAL fill: cancel the resting remainder so the order can't fill MORE after the
            # Book has saved `filled` (which would silently diverge Book from exchange). The next
            # cycle re-plans the still-missing delta from exchange truth under a fresh (unique) coid.
            logger.warning("LIVE partial fill %s: %d/%d @ %d¢ (id=%s) — cancelling resting remainder",
                           tgt.ticker, filled, tgt.target_contracts, price_cents, coid)
            await _cancel_quietly(client, oid, tgt.ticker)
        # W4: prefer the exchange-reported fee from the order ack when it corresponds to THIS
        # fill quantity; otherwise fall back to the ceil(7%·N·P·(1−P)) estimate. The ack fee is
        # only trustworthy when the ack's fill_count matches what the position poll confirmed
        # (they can diverge: the ack snapshots fill-at-ack-time, the poll is the settled truth).
        # W4 RECON: this per-order ack fee is the INTERIM per-fill value; engine._reconcile_book_fees
        #   overwrites it with the exact per-position fees_paid_dollars from GET /portfolio/positions
        #   at END OF CYCLE (KalshiPosition.fees_paid_cents), so settlement subtracts the exact fee.
        ack_fee, ack_fill = order_ack_fee_cents(ack)
        if ack_fee is not None and ack_fill == filled:
            fee_cents = ack_fee
        else:
            fee_cents = trade_fee_cents(price_cents / 100.0, filled)
        return {
            "ticker":            tgt.ticker,
            "side":              tgt.side,
            "contracts":         filled,
            "fill_price_cents":  price_cents,
            "fee_cents":         fee_cents,
            "kalshi_order_id":   oid,
            "ack":               ack,
        }

    # PAPER fill at the configured price model.
    fee = trade_fee_cents(fill_price_cents / 100.0, tgt.target_contracts)
    return {
        "ticker":           tgt.ticker,
        "side":             tgt.side,
        "contracts":        tgt.target_contracts,
        "fill_price_cents": int(fill_price_cents),
        "fee_cents":        int(fee),
        "kalshi_order_id":  None,
        "ack":              None,
    }


async def _cancel_quietly(client: KalshiClient, order_id: str | None, ticker: str) -> None:
    """Best-effort cancel of a resting/partially-filled order; a failure logs but never raises
    (if it filled in the race the #3 exchange-truth delta corrects sizing next cycle anyway)."""
    if not order_id:
        return
    try:
        await client.cancel_order(order_id)
    except Exception:
        logger.exception("LIVE: cancel of order %s (id=%s) failed", ticker, order_id)


def _paper_fill_cents(cfg: Config, c: KalshiContract, tgt: TargetPosition) -> int:
    """The cents price we assume PAPER mode pays."""
    if tgt.side == "yes":
        if cfg.execution.fill_model == "ask":
            base = c.yes_ask
        elif cfg.execution.fill_model == "bid":
            base = c.yes_bid
        else:
            base = c.yes_mid
    else:
        if cfg.execution.fill_model == "ask":
            base = c.no_ask
        elif cfg.execution.fill_model == "bid":
            base = c.no_bid
        else:
            base = c.no_mid
    return int(round(base * 100)) + int(cfg.execution.slippage_cents)


# ---------------------------------------------------------------------------
# Risk gates
# ---------------------------------------------------------------------------

def _kill_switch_armed(cfg: Config) -> bool:
    """True if the kill-switch file exists. Cheap stat() — safe to call per leg.

    The file is the cross-process signal: any terminal, the TUI ('k'), or
    `python scripts/kill.py` can arm it WHILE a cycle is mid-flight, and the
    per-leg check in `execute()` will halt before the next order is placed.
    """
    return Path(cfg.paths.kill_switch).exists()


def _check_kill_switch(cfg: Config) -> None:
    """Pre-cycle gate: hard-abort before any work if already armed."""
    if _kill_switch_armed(cfg):
        msg = f"KILL_SWITCH present at {cfg.paths.kill_switch} — refusing to trade"
        logger.error(msg)
        raise KillSwitchTripped(msg)


def check_daily_loss(cfg: Config, book: Book) -> None:
    """I2: ONE account-wide gate on REALIZED daily loss, checked per CYCLE (was N per-market raises).
    daily_loss_cents is a single account-wide accumulator keyed by anchor date; gate the whole
    cycle if a RECENT date has breached the cap (the realized loss that breaches it is keyed to the
    day it settled). W5 note: realized loss accrues only at settlement (next-day for a daily market),
    so for the CURRENT anchor this is ~always 0 at decision time — the real same-day stops are the
    intraday outlay breaker (C3) + the exposure cap (C2). Kept as belt-and-suspenders.

    I-A: only RECENT breached dates gate. daily_loss_cents accumulates per anchor date and is never
    cleared, so scanning ALL of history would latch the bot off PERMANENTLY (no reset path) on a
    single old >cap day. A 7-day rolling window self-clears (a fresh breach is ~1 day old since
    loss lands at T+1), so a bad day stops trading for ~a week rather than forever."""
    cap_cents = -int(round(cfg.risk.daily_max_loss_usd * 100))
    cutoff = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    for date_iso, daily in book.daily_loss_cents.items():
        if date_iso < cutoff:           # I-A: old breaches age out — no permanent latch
            continue
        if daily <= cap_cents:
            msg = (f"daily loss cap hit for {date_iso}: {daily}¢ <= {cap_cents}¢ "
                   f"(${cfg.risk.daily_max_loss_usd:.2f})")
            logger.error(msg)
            raise KillSwitchTripped(msg)


# ---------------------------------------------------------------------------
# LogRow factories
# ---------------------------------------------------------------------------

def _t_utc_for(anchor_date: pd.Timestamp, cfg: Config, tz: str | None = None) -> pd.Timestamp:
    # I6: give tz_localize a DST policy so a non-1PM anchor that lands in a spring-forward gap or
    # a fall-back ambiguous hour can't raise (the 1 PM anchor is clear of both, so this is a no-op
    # today). ambiguous=True -> DST side; nonexistent -> shift forward out of the gap.
    return (pd.Timestamp(anchor_date).normalize()
            .tz_localize(tz or cfg.local_tz, ambiguous=True, nonexistent="shift_forward")
            + pd.Timedelta(hours=cfg.model.anchor_hour_local)).tz_convert("UTC")


def _base_row(cfg, run_utc, t_utc, pred, c: KalshiContract) -> dict:
    return dict(
        run_utc=run_utc,
        anchor_date=pd.Timestamp(pred.date),
        model=pred.model,
        mode=cfg.mode,
        t_utc=t_utc,
        model_median=pred.median,
        model_lo10=pred.lo10,
        model_hi90=pred.hi90,
        ticker=c.ticker,
        bucket_spec=c.bucket_spec,
        subtitle=c.subtitle,
        status=c.status,
        is_live=c.is_live,
        yes_bid=c.yes_bid, yes_ask=c.yes_ask, no_bid=c.no_bid, no_ask=c.no_ask,
        yes_mid=c.yes_mid, no_mid=c.no_mid, last_price=c.last_price,
    )


def _no_target_row(cfg, run_utc, t_utc, pred, c) -> LogRow:
    base = _base_row(cfg, run_utc, t_utc, pred, c)
    return LogRow(
        **base, p_model=0.0, p_market=None,
        best_side=None, target_contracts=0, entry_price_cents=None,
        net_ev_cents=None, kelly_f=None, scaled_kelly=None, exposure_frac=None,
        throttle=None,
        fill_status="no_target", fill_price_cents=None, fill_contracts=0, fee_cents=None,
    )


def _skip_row(cfg, run_utc, t_utc, pred, c, tgt: TargetPosition, *, reason: str) -> LogRow:
    base = _base_row(cfg, run_utc, t_utc, pred, c)
    r = tgt.rationale
    return LogRow(
        **base, p_model=float(r.get("p_model", 0.0)), p_market=r.get("p_market"),
        best_side=tgt.side, target_contracts=tgt.target_contracts,
        entry_price_cents=tgt.limit_price_cents,
        net_ev_cents=r.get("net_ev_cents"), kelly_f=r.get("kelly_f"),
        scaled_kelly=r.get("scaled_kelly"), exposure_frac=r.get("exposure_frac"),
        throttle=r.get("throttle"),
        fill_status=reason, fill_price_cents=None, fill_contracts=0, fee_cents=None,
    )


def _filled_row(cfg, run_utc, t_utc, pred, c, tgt, fill) -> LogRow:
    base = _base_row(cfg, run_utc, t_utc, pred, c)
    r = tgt.rationale
    return LogRow(
        **base, p_model=float(r.get("p_model", 0.0)), p_market=r.get("p_market"),
        best_side=tgt.side, target_contracts=tgt.target_contracts,
        entry_price_cents=tgt.limit_price_cents,
        net_ev_cents=r.get("net_ev_cents"), kelly_f=r.get("kelly_f"),
        scaled_kelly=r.get("scaled_kelly"), exposure_frac=r.get("exposure_frac"),
        throttle=r.get("throttle"),
        fill_status="filled", fill_price_cents=fill["fill_price_cents"],
        fill_contracts=fill["contracts"], fee_cents=fill["fee_cents"],
    )


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def reconcile_settlements(book: Book, cli_df: pd.DataFrame, *, station: str | None = None) -> int:
    """Walk open positions; if CLI truth exists for their anchor_date, settle.

    `station` (multi-market): only settle positions whose station matches — so each city's
    positions resolve against ITS OWN station's high. None settles every position (legacy
    single-station / model path). Returns the cents of newly realized PnL this call.
    """
    truth_by_date = {pd.Timestamp(d).normalize().date(): int(v)
                     for d, v in cli_df[["date", "max_temp_f"]].dropna().itertuples(index=False, name=None)}
    realized = 0
    for key in list(book.positions.keys()):
        p = book.positions[key]
        if p.settled:
            continue
        if station is not None and p.station != station:
            continue
        d = pd.Timestamp(p.anchor_date).date()
        if d not in truth_by_date:
            continue
        actual = truth_by_date[d]
        from weather_alpha.pmf import parse_bucket
        pred, _ = parse_bucket(p.bucket_spec)
        bucket_wins = pred(actual)
        won = (p.side == "yes" and bucket_wins) or (p.side == "no" and not bucket_wins)
        pnl = book.settle(p.ticker, p.side, won, settled_utc=pd.Timestamp.now(tz="UTC").isoformat())
        realized += pnl
        report(f"[{p.station or '?'} {d:%d%b}] 💰 SETTLED {p.side.upper()} {p.bucket_spec} — "
               f"high {actual}°F → {'WON' if won else 'lost'} {usd_signed(pnl)}")
    return realized
