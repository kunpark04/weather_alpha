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
from weather_alpha.kalshi import KalshiClient, KalshiContract
from weather_alpha.live_log import LogRow, append_rows
from weather_alpha.model import Prediction
from weather_alpha.positions import Book
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
) -> ExecutionResult:
    """Run execution for one anchor cycle. Mutates `book` in place. Appends log rows.

    `market` (multi-market) supplies the settlement station tagged onto each fill and the
    local tz for the log timestamp; when None, the legacy cfg.station/cfg.local_tz are used.
    `bankroll_usd` (the running account balance) enables the process-wide exposure cap; when
    0 the cap is skipped (non-engine callers) — the daily-outlay cap still applies.
    """
    _check_kill_switch(cfg)
    _check_daily_loss(cfg, book, prediction)
    station = market.station if market is not None else cfg.station
    tz = market.local_tz if market is not None else cfg.local_tz
    # #3: for LIVE, size deltas against EXCHANGE truth (not the local Book), so a late fill /
    # manual trade / stale snapshot can't cause over- or under-buying. One snapshot per cycle; on
    # fetch failure fall back to the Book (live_held stays None).
    live_held: dict[tuple[str, str], int] | None = None
    if cfg.is_live() and client is not None:
        try:
            live_held = {(p.ticker, p.side): p.contracts for p in await client.get_positions()}
        except Exception:
            logger.exception("exposure: get_positions failed; using Book for deltas this cycle")

    target_by_ticker = {t.ticker: t for t in strategy.targets}
    rows: list[LogRow] = []
    fills = 0
    skipped = 0
    attempts = 0          # #10: order ATTEMPTS placed (fill or not) — the real per-anchor backstop
    orders: list[dict] = []

    run_utc = pd.Timestamp.now(tz="UTC")
    t_utc = _t_utc_for(prediction.date, cfg, tz)
    anchor_iso = str(prediction.date.date())
    halted = False
    halt_reason: str | None = None

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

        # Target-vs-current diff. Strategy emits absolute targets ("hold 646 YES");
        # execution only fills the delta. Without this, intraday refreshes compound
        # positions on every cycle.
        if live_held is not None:
            current_qty = live_held.get((tgt.ticker, tgt.side), 0)        # #3: exchange truth (LIVE)
        else:
            current = book.open_for(tgt.ticker, tgt.side)
            current_qty = current.contracts if (current and not current.settled) else 0
        delta_qty = tgt.target_contracts - current_qty
        if delta_qty <= 0:
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt, reason="at_target"))
            skipped += 1
            continue
        tgt_delta = replace(tgt, target_contracts=delta_qty)

        # Re-check the kill switch before EVERY order (C3). Lets an operator halt a
        # multi-leg cycle the instant they arm it, not just at cycle start.
        if _kill_switch_armed(cfg):
            logger.error("KILL_SWITCH armed mid-cycle — halting before %s and all "
                         "remaining legs (%d filled so far)", c.ticker, fills)
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt_delta,
                                  reason="kill_switch"))
            skipped += 1
            halted, halt_reason = True, "kill_switch"
            break

        # Per-anchor trade cap (W6): never place more entries than configured,
        # whatever the strategy emits. Runaway-order backstop.
        if attempts >= cfg.risk.per_anchor_max_trades:
            logger.error("per_anchor_max_trades=%d reached (order attempts) — halting before %s",
                         cfg.risk.per_anchor_max_trades, c.ticker)
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt_delta,
                                  reason="max_trades"))
            skipped += 1
            halted, halt_reason = True, "max_trades"
            break

        # Intraday circuit breaker (C3): before settlement the only honest measure of
        # "loss at risk" is total stake outlaid today (worst case = lose every cent of
        # it). daily_loss_cents only accrues at next-day settlement, so it cannot gate
        # intraday; outlay-at-risk is the correct same-day proxy. Halt new entries once
        # today's outlay would exceed risk.daily_max_loss_usd.
        delta_cost_cents = delta_qty * tgt_delta.limit_price_cents
        cap_cents = int(round(cfg.risk.daily_max_loss_usd * 100))
        outlay_cents = book.daily_outlay_cents(anchor_iso)
        if outlay_cents + delta_cost_cents > cap_cents:
            logger.error("daily outlay cap: %d¢ + %d¢ > %d¢ ($%.2f) — halting before %s",
                         outlay_cents, delta_cost_cents, cap_cents,
                         cfg.risk.daily_max_loss_usd, c.ticker)
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt_delta,
                                  reason="daily_outlay_cap"))
            skipped += 1
            halted, halt_reason = True, "daily_outlay_cap"
            break

        # C2 — process-wide exposure cap: cumulative OPEN stake across ALL cities (the shared
        # Book) must stay within total_exposure_max_pct of the running balance, so N markets
        # can't each deploy the full account. Skipped when bankroll_usd is unknown (0).
        if bankroll_usd > 0:
            exp_cap_cents = int(round(cfg.strategy.total_exposure_max_pct * bankroll_usd * 100))
            if book.exposure_cents() + delta_cost_cents > exp_cap_cents:
                logger.error("exposure cap: open %d¢ + %d¢ > %d¢ (%.0f%% of $%.2f) — halting before %s",
                             book.exposure_cents(), delta_cost_cents, exp_cap_cents,
                             cfg.strategy.total_exposure_max_pct * 100, bankroll_usd, c.ticker)
                rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt_delta,
                                      reason="exposure_cap"))
                skipped += 1
                halted, halt_reason = True, "exposure_cap"
                break

        # W4: an order-placement exception must NOT abort the cycle (which would leave a
        # half-filled wing and skip the Book save below). Catch it, record the leg as
        # errored, and stop placing further legs. Already-filled legs are persisted by the
        # book.save below; the deterministic wa- client_order_id (C4) makes a next-cycle
        # retry of an unbooked leg idempotent at the exchange.
        attempts += 1   # #10: a submitted order consumes the cap whether or not it fills
        try:
            fill = await _execute_one(cfg, c, tgt_delta, client, anchor_date=anchor_iso)
        except Exception:
            logger.exception("leg %s raised in _execute_one — halting cycle; "
                             "already-filled legs are persisted", c.ticker)
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt_delta, reason="error"))
            skipped += 1
            halted, halt_reason = True, "leg_error"
            break
        if fill is None:
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt_delta, reason="rejected"))
            skipped += 1
            continue

        book.add_fill(
            ticker=c.ticker, side=tgt_delta.side,
            contracts=fill["contracts"], fill_cents=fill["fill_price_cents"],
            fee_cents=fill["fee_cents"], bucket_spec=tgt_delta.bucket_spec,
            opened_utc=run_utc.isoformat(), anchor_date=anchor_iso,
            station=station,
        )
        fills += 1
        orders.append(fill)
        rows.append(_filled_row(cfg, run_utc, t_utc, prediction, c, tgt_delta, fill))

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
            "halted_mid_cycle": halted,
            "halt_reason": halt_reason,
        },
    )


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
                       client: KalshiClient | None, *, anchor_date: str) -> dict | None:
    fill_price_cents = _paper_fill_cents(cfg, c, tgt)
    if fill_price_cents > tgt.limit_price_cents:
        # Won't pay through our limit — same logic for paper + live.
        return None

    if cfg.is_live():
        if client is None:
            raise RuntimeError("LIVE mode requires an authenticated KalshiClient")
        # C4 — deterministic idempotency key: the same logical leg on the same anchor
        # date always yields the same client_order_id, so a retry or a re-run of the
        # cycle collides on Kalshi's uniqueness check instead of doubling the position.
        # W3 trade-off: on a PARTIAL fill, next cycle retries the remainder with this SAME coid
        # + a new count — Kalshi may reject it (remainder unplaced) rather than double-fill. We
        # keep the safe under-fill; a unique-per-attempt coid would place the remainder but
        # reintroduce double-order risk. Revisit with a live partial-fill test before changing.
        coid = f"wa-{anchor_date}-{tgt.ticker}-{tgt.side}"
        # Snapshot true exchange holdings BEFORE the order so we can measure the real fill.
        before_qty, before_avg = await _live_held(client, tgt.ticker, tgt.side)
        ack = await client.place_order(
            ticker=tgt.ticker, side=tgt.side, action="buy",
            count=tgt.target_contracts, limit_price_cents=tgt.limit_price_cents,
            client_order_id=coid,
        )
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
            oid = ack.get("order_id") or (ack.get("order") or {}).get("order_id")
            try:
                if oid:
                    await client.cancel_order(oid)
            except Exception:
                logger.exception("LIVE: cancel of unfilled order %s (id=%s) failed", tgt.ticker, oid)
            logger.warning("LIVE order %s (id=%s) no fill after %d polls; cancelled, retry next cycle",
                           tgt.ticker, coid, _FILL_POLL_TRIES)
            return None
        if filled < tgt.target_contracts:
            logger.warning("LIVE partial fill %s: %d/%d @ %d¢ (id=%s)", tgt.ticker,
                           filled, tgt.target_contracts, price_cents, coid)
        return {
            "ticker":            tgt.ticker,
            "side":              tgt.side,
            "contracts":         filled,
            "fill_price_cents":  price_cents,
            # W2: FORMULA fee, not Kalshi's actually-charged fee (they can differ slightly). The
            # post-settlement get_balance() re-sync reconciles the true balance; this per-position
            # fee stays an estimate until we read the exchange fee from fills (HANDOFF AUDIT #8).
            "fee_cents":         trade_fee_cents(price_cents / 100.0, filled),
            "kalshi_order_id":   ack.get("order_id"),
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


def _check_daily_loss(cfg: Config, book: Book, prediction: Prediction) -> None:
    """Pre-cycle gate on REALIZED loss for this anchor date. W5 note: realized loss accrues only
    at settlement (next-day for a daily market), so for the CURRENT anchor this is ~always 0 at
    decision time — the real same-day stops are the intraday outlay breaker (C3) + the exposure
    cap (C2), not this. Kept as a belt-and-suspenders for an already-settled date."""
    date_iso = str(prediction.date.date())
    daily = book.daily_loss_cents.get(date_iso, 0)
    cap_cents = -int(round(cfg.risk.daily_max_loss_usd * 100))
    if daily <= cap_cents:
        msg = (f"daily loss cap hit for {date_iso}: {daily}¢ <= {cap_cents}¢ "
               f"(${cfg.risk.daily_max_loss_usd:.2f})")
        logger.error(msg)
        raise KillSwitchTripped(msg)


# ---------------------------------------------------------------------------
# LogRow factories
# ---------------------------------------------------------------------------

def _t_utc_for(anchor_date: pd.Timestamp, cfg: Config, tz: str | None = None) -> pd.Timestamp:
    return (pd.Timestamp(anchor_date).normalize().tz_localize(tz or cfg.local_tz)
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
