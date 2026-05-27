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

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import pandas as pd

from forecast_alpha.config import Config
from forecast_alpha.fees import trade_fee_cents
from forecast_alpha.kalshi import KalshiClient, KalshiContract
from forecast_alpha.live_log import LogRow, append_rows
from forecast_alpha.model import Prediction
from forecast_alpha.positions import Book
from forecast_alpha.strategy import StrategyOutput, TargetPosition

logger = logging.getLogger(__name__)


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
) -> ExecutionResult:
    """Run execution for one anchor cycle. Mutates `book` in place. Appends log rows."""
    _check_kill_switch(cfg)
    _check_daily_loss(cfg, book, prediction)

    target_by_ticker = {t.ticker: t for t in strategy.targets}
    rows: list[LogRow] = []
    fills = 0
    skipped = 0
    orders: list[dict] = []

    run_utc = pd.Timestamp.now(tz="UTC")
    t_utc = _t_utc_for(prediction.date, cfg)

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

        fill = await _execute_one(cfg, c, tgt, client)
        if fill is None:
            rows.append(_skip_row(cfg, run_utc, t_utc, prediction, c, tgt, reason="rejected"))
            skipped += 1
            continue

        book.add_fill(
            ticker=c.ticker, side=tgt.side,
            contracts=fill["contracts"], fill_cents=fill["fill_price_cents"],
            fee_cents=fill["fee_cents"], bucket_spec=tgt.bucket_spec,
            opened_utc=run_utc.isoformat(), anchor_date=str(prediction.date.date()),
        )
        fills += 1
        orders.append(fill)
        rows.append(_filled_row(cfg, run_utc, t_utc, prediction, c, tgt, fill))

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
        },
    )


# ---------------------------------------------------------------------------
# Per-contract execution
# ---------------------------------------------------------------------------

async def _execute_one(cfg: Config, c: KalshiContract, tgt: TargetPosition,
                       client: KalshiClient | None) -> dict | None:
    fill_price_cents = _paper_fill_cents(cfg, c, tgt)
    if fill_price_cents > tgt.limit_price_cents:
        # Won't pay through our limit — same logic for paper + live.
        return None

    if cfg.is_live():
        if client is None:
            raise RuntimeError("LIVE mode requires an authenticated KalshiClient")
        ack = await client.place_order(
            ticker=tgt.ticker, side=tgt.side, action="buy",
            count=tgt.target_contracts, limit_price_cents=tgt.limit_price_cents,
        )
        # Kalshi acks the order; actual fill data may come from a separate poll.
        # For v1 we treat the limit price as the fill price (worst case at our limit).
        return {
            "ticker":            tgt.ticker,
            "side":              tgt.side,
            "contracts":         tgt.target_contracts,
            "fill_price_cents":  tgt.limit_price_cents,
            "fee_cents":         trade_fee_cents(tgt.limit_price_cents / 100.0, tgt.target_contracts),
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

def _check_kill_switch(cfg: Config) -> None:
    if Path(cfg.paths.kill_switch).exists():
        msg = f"KILL_SWITCH present at {cfg.paths.kill_switch} — refusing to trade"
        logger.error(msg)
        raise KillSwitchTripped(msg)


def _check_daily_loss(cfg: Config, book: Book, prediction: Prediction) -> None:
    """Refuse to open new positions if today's realized loss exceeds the cap."""
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

def _t_utc_for(anchor_date: pd.Timestamp, cfg: Config) -> pd.Timestamp:
    return (pd.Timestamp(anchor_date).normalize().tz_localize(cfg.local_tz)
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

def reconcile_settlements(book: Book, cli_df: pd.DataFrame) -> int:
    """Walk all open positions; if cli truth exists for their anchor_date, settle.

    Returns the cents of newly realized PnL this call.
    """
    truth_by_date = {pd.Timestamp(d).normalize().date(): int(v)
                     for d, v in cli_df[["date", "max_temp_f"]].dropna().itertuples(index=False, name=None)}
    realized = 0
    for key in list(book.positions.keys()):
        p = book.positions[key]
        if p.settled:
            continue
        d = pd.Timestamp(p.anchor_date).date()
        if d not in truth_by_date:
            continue
        actual = truth_by_date[d]
        from forecast_alpha.pmf import parse_bucket
        pred, _ = parse_bucket(p.bucket_spec)
        bucket_wins = pred(actual)
        won = (p.side == "yes" and bucket_wins) or (p.side == "no" and not bucket_wins)
        realized += book.settle(p.ticker, p.side, won, settled_utc=pd.Timestamp.now(tz="UTC").isoformat())
    return realized
