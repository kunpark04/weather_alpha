"""Daily settlement reconciler.

Run once a day (suggested: ~7 AM CT, after NWS CLI publishes the previous day's
report). Refreshes data, settles any open positions whose anchor_date now has
CLI truth, persists the updated book, and prints a daily-PnL summary.

Usage:
    python scripts/settle.py              # standard reconcile + summary
    python scripts/settle.py --quiet      # no per-position detail, summary only
    python scripts/settle.py --no-refresh # use cached CLI parquet
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import pandas as pd

# Allow running this script from anywhere — add project root to path.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from forecast_alpha.config import load_config
from forecast_alpha.data import load_bundle
from forecast_alpha.engine import refresh_data
from forecast_alpha.execution import reconcile_settlements
from forecast_alpha.log import setup_logging
from forecast_alpha.positions import Book


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--quiet", action="store_true", help="summary only, no per-position detail")
    p.add_argument("--no-refresh", action="store_true", help="use cached CLI parquet")
    args = p.parse_args(argv)

    cfg = load_config()
    setup_logging(cfg.paths.logs_dir)
    book = Book.load(cfg.paths.positions_snapshot)

    n_open_before = sum(1 for p in book.positions.values() if not p.settled)
    pre_realized = book.realized_pnl_cents
    print(f"Before:  open positions = {n_open_before}  "
          f"realized total = ${pre_realized / 100:+.2f}  "
          f"fees paid = ${book.fees_paid_cents / 100:+.2f}")

    if not args.no_refresh:
        await refresh_data(cfg)

    bundle = load_bundle(cfg.paths.data_dir, cfg.station)
    delta = reconcile_settlements(book, bundle.cli)
    book.save(cfg.paths.positions_snapshot)

    n_open_after = sum(1 for p in book.positions.values() if not p.settled)
    print(f"After:   open positions = {n_open_after}  "
          f"realized total = ${book.realized_pnl_cents / 100:+.2f}  "
          f"this run = ${delta / 100:+.2f}")

    # Per-anchor-date PnL
    by_date: dict[str, list] = {}
    for pos in book.positions.values():
        if not pos.settled:
            continue
        by_date.setdefault(pos.anchor_date, []).append(pos)

    if by_date:
        print("\nDaily realized PnL (settled anchors only):")
        print(f"  {'date':<12} {'n':>3} {'gross_cents':>12} {'fees':>8} {'net':>10} {'win%':>6}")
        for date in sorted(by_date.keys()):
            positions = by_date[date]
            gross = sum(p.realized_pnl_cents + p.total_fees_cents for p in positions)
            fees = sum(p.total_fees_cents for p in positions)
            net = sum(p.realized_pnl_cents for p in positions)
            wins = sum(1 for p in positions if p.realized_pnl_cents > 0)
            win_pct = 100.0 * wins / len(positions)
            print(f"  {date:<12} {len(positions):>3d} {gross:>+12d} "
                  f"{fees:>8d} {net:>+10d} {win_pct:>5.0f}%")

    if not args.quiet and by_date:
        print("\nPer-position detail (most recent first):")
        all_settled = sorted(
            [p for p in book.positions.values() if p.settled],
            key=lambda p: p.anchor_date,
            reverse=True,
        )
        for pos in all_settled[:30]:
            mark = "WIN" if pos.realized_pnl_cents > 0 else "LOSS"
            print(f"  {pos.anchor_date}  {pos.ticker:<28}  {pos.side:>3}  "
                  f"qty={pos.contracts:>5}  avg={pos.avg_cost_cents:>5.1f}¢  "
                  f"net={pos.realized_pnl_cents:>+7d}¢  [{mark}]")

    if n_open_after > 0:
        print(f"\n{n_open_after} position(s) still open (CLI truth not yet available for their anchor_date).")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
