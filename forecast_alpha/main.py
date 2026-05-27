"""Entry point. Launches the Textual app (default) or runs one cycle in headless mode.

Usage:
    python -m forecast_alpha                    # launch the TUI (production)
    python -m forecast_alpha --headless         # one-shot cycle, print summary, exit
    python -m forecast_alpha --headless --loop  # headless scheduler loop (no TUI)
    python -m forecast_alpha --anchor 2026-05-22  # force anchor date
    forecast-alpha                              # same as `python -m forecast_alpha`
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import pandas as pd

from forecast_alpha import __version__
from forecast_alpha.config import load_config
from forecast_alpha.engine import refresh_data, run_cycle
from forecast_alpha.kalshi import KalshiClient
from forecast_alpha.log import setup_logging
from forecast_alpha.model import load_artifacts
from forecast_alpha.positions import Book
from forecast_alpha.scheduler import Action, Scheduler

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="forecast-alpha", description="Kalshi KXHIGHCHI trading bot")
    p.add_argument("--config", help="path to YAML config (else default + env override)")
    p.add_argument("--headless", action="store_true", help="run without the TUI")
    p.add_argument("--loop", action="store_true", help="(headless) keep scheduler running")
    p.add_argument("--anchor", help="force anchor date YYYY-MM-DD (one-shot only)")
    p.add_argument("--no-refresh", action="store_true", help="skip data refresh subprocess")
    p.add_argument("--version", action="version", version=__version__)
    return p.parse_args(argv)


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(cfg.paths.logs_dir)
    logger.info("forecast-alpha v%s starting in %s mode", __version__, cfg.mode.upper())

    art = load_artifacts(cfg.paths.model_dir)
    book = Book.load(cfg.paths.positions_snapshot)
    logger.info("loaded book: %d open / %d total positions, realized=%+d¢",
                sum(1 for p in book.positions.values() if not p.settled),
                len(book.positions), book.realized_pnl_cents)

    if args.headless:
        return asyncio.run(_run_headless(cfg, art, book, args))
    return _run_tui(cfg, art, book)


# ---------------------------------------------------------------------------
# TUI entry
# ---------------------------------------------------------------------------

def _run_tui(cfg, art, book) -> int:
    from forecast_alpha.tui import ForecastAlphaApp
    app = ForecastAlphaApp(cfg, art, book)
    app.run()
    return 0


# ---------------------------------------------------------------------------
# Headless entries
# ---------------------------------------------------------------------------

async def _run_headless(cfg, art, book, args) -> int:
    force_anchor = pd.Timestamp(args.anchor).normalize() if args.anchor else None

    async with KalshiClient(cfg.kalshi, authenticated=cfg.is_live()) as kalshi:
        if not args.loop:
            result = await run_cycle(cfg, art, book, kalshi,
                                     force_anchor=force_anchor,
                                     skip_refresh=args.no_refresh)
            _print_summary(result)
            return 0

        scheduler = Scheduler(cfg)
        logger.info("headless loop starting; ctrl-C to stop")
        try:
            while True:
                action = scheduler.due()
                if action == Action.DATA_REFRESH:
                    await refresh_data(cfg)
                    scheduler.record(action)
                elif action in (Action.ANCHOR, Action.INTRADAY):
                    result = await run_cycle(cfg, art, book, kalshi,
                                             skip_refresh=(action == Action.INTRADAY))
                    _print_summary(result)
                    scheduler.record(action)
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            logger.info("headless loop interrupted; exiting cleanly")
            return 0


def _print_summary(r) -> None:
    print(f"\n=== cycle @ anchor={r.anchor_date.date()} ===")
    print(f"  median {r.prediction.median}°F  80% CI [{r.prediction.lo10}, {r.prediction.hi90}]°F  "
          f"peak {r.prediction.peak_F}°F P={r.prediction.peak_P:.3f}  season={r.prediction.season}")
    print(f"  contracts: {r.contracts_count} ({r.live_contracts} live)")
    print(f"  strategy:  {r.strategy.diagnostics}")
    print(f"  execution: fills={r.execution.fills} skipped={r.execution.skipped}")
    if r.realized_at_settle:
        print(f"  settle:    {r.realized_at_settle:+d}¢ realized this cycle")


if __name__ == "__main__":
    sys.exit(run())
