"""Entry point. Launches the Textual app (default) or runs one cycle in headless mode.

Usage:
    python -m weather_alpha                    # launch the TUI (production)
    python -m weather_alpha --headless         # one-shot cycle, print summary, exit
    python -m weather_alpha --headless --loop  # headless scheduler loop (no TUI)
    python -m weather_alpha --anchor 2026-05-22  # force anchor date
    weather-alpha                              # same as `python -m weather_alpha`
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import pandas as pd

from weather_alpha import __version__
from weather_alpha.config import load_config
from weather_alpha.engine import preflight, refresh_data, run_cycle
from weather_alpha.kalshi import KalshiClient
from weather_alpha.log import setup_logging
from weather_alpha.model import load_artifacts
from weather_alpha.positions import Book
from weather_alpha.scheduler import Action, Scheduler

logger = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="weather-alpha", description="Kalshi KXHIGHCHI trading bot")
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
    logger.info("weather-alpha v%s starting in %s mode", __version__, cfg.mode.upper())

    # Model-free strategies (market_wing) never touch the model, so require NO artifacts on
    # disk — the live host needs zero model data. Load only when the model is actually enabled.
    art = load_artifacts(cfg.paths.model_dir) if cfg.model.enabled else None
    if art is None:
        logger.info("model-free mode (model.enabled=false): skipping artifact load")
    book = Book.load(cfg.paths.positions_snapshot)
    logger.info("loaded book: %d open / %d total positions, realized=%+d¢",
                sum(1 for p in book.positions.values() if not p.settled),
                len(book.positions), book.realized_pnl_cents)
    # Migrate any legacy position (pre-multimarket, station="") to the first market's station.
    if book.backfill_station(cfg.markets[0].station):
        book.save(cfg.paths.positions_snapshot)

    if args.headless:
        return asyncio.run(_run_headless(cfg, art, book, args))
    return _run_tui(cfg, art, book)


# ---------------------------------------------------------------------------
# TUI entry
# ---------------------------------------------------------------------------

def _run_tui(cfg, art, book) -> int:
    from weather_alpha.tui import WeatherAlphaApp
    app = WeatherAlphaApp(cfg, art, book)
    app.run()
    return 0


# ---------------------------------------------------------------------------
# Headless entries
# ---------------------------------------------------------------------------

async def _run_headless(cfg, art, book, args) -> int:
    force_anchor = pd.Timestamp(args.anchor).normalize() if args.anchor else None

    async with KalshiClient(cfg.kalshi, authenticated=cfg.is_live()) as kalshi:
        # Activation preflight (read-only): wallet, open positions, today's market, strategy.
        await preflight(cfg, book, kalshi)
        if not args.loop:
            results = await run_cycle(cfg, art, book, kalshi,
                                      force_anchor=force_anchor,
                                      skip_refresh=args.no_refresh)
            for result in results:
                _print_summary(result)
            return 0

        if not cfg.model.enabled:
            # Model-free multi-market: one resident process fires each city at ITS OWN local
            # 1 PM (any US tz). No data-refresh / intraday (model-free fetches nothing).
            return await _run_market_loop(cfg, art, book, kalshi)

        # Model path (single-station): data-refresh + anchor + intraday on the single-tz Scheduler.
        scheduler = Scheduler(cfg, cfg.paths.scheduler_state)
        logger.info("headless loop starting; ctrl-C to stop")
        try:
            while True:
                action = scheduler.due()
                if action == Action.DATA_REFRESH:
                    await refresh_data(cfg)
                    scheduler.record(action)
                elif action in (Action.ANCHOR, Action.INTRADAY):
                    results = await run_cycle(cfg, art, book, kalshi,
                                              skip_refresh=(action == Action.INTRADAY))
                    for result in results:
                        _print_summary(result)
                    scheduler.record(action)
                await asyncio.sleep(60)
        except KeyboardInterrupt:
            logger.info("headless loop interrupted; exiting cleanly")
            return 0


async def _run_market_loop(cfg, art, book, kalshi) -> int:
    """Resident model-free loop: fire each market at ITS OWN local 1 PM, any US tz, one process.

    Sleeps until the soonest market anchor (capped at 5 min so day-rollover / DST / a newly
    edited market list are picked up), then trades + settles every market that just came due.
    """
    from weather_alpha.scheduler import MarketAnchorScheduler

    sched = MarketAnchorScheduler(cfg, cfg.paths.scheduler_state)
    tzs = sorted({m.local_tz for m in cfg.markets})
    logger.info("multi-market loop: %d market(s) across %s; ctrl-C to stop",
                len(cfg.markets), ", ".join(tzs))
    try:
        while True:
            due = sched.due_markets(cfg.markets)
            if due:
                logger.info("anchor due: %s", [m.name for m in due])
                results = await run_cycle(cfg, art, book, kalshi, markets=due)
                for result in results:
                    _print_summary(result)
                # #2: record ONLY markets that produced a result. run_cycle drops a market that
                # threw (per-market isolation), so an errored city stays un-recorded and retries
                # next tick instead of being silently skipped until tomorrow.
                done = {r.diagnostics.get("market") for r in results}
                for m in due:
                    if m.name in done:
                        sched.record(m)
            sleep_s = max(1.0, min(sched.seconds_until_next(cfg.markets), 300.0))
            await asyncio.sleep(sleep_s)
    except KeyboardInterrupt:
        logger.info("multi-market loop interrupted; exiting cleanly")
        return 0


def _print_summary(r) -> None:
    print(f"\n=== cycle [{r.diagnostics.get('market', '?')}] @ anchor={r.anchor_date.date()} ===")
    print(f"  median {r.prediction.median}°F  80% CI [{r.prediction.lo10}, {r.prediction.hi90}]°F  "
          f"peak {r.prediction.peak_F}°F P={r.prediction.peak_P:.3f}  season={r.prediction.season}")
    print(f"  contracts: {r.contracts_count} ({r.live_contracts} live)")
    print(f"  strategy:  {r.strategy.diagnostics}")
    print(f"  execution: fills={r.execution.fills} skipped={r.execution.skipped}")
    if r.realized_at_settle:
        print(f"  settle:    {r.realized_at_settle:+d}¢ realized this cycle")


if __name__ == "__main__":
    sys.exit(run())
