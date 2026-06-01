"""Drawdown-halt control — status / reset, from any terminal.

The per-city (25%) and whole-account (50%) drawdown circuit-breakers LATCH: once tripped,
that city (or the whole account) places no new trades until reset here. Settlement of
existing positions still runs while halted. The halt state lives in the Book
(paths.positions_snapshot), so this always matches what the running bot enforces.

Usage (run from project root) — --config is REQUIRED and MUST be the RUNNING bot's config,
because halt state lives in that config's positions.json (data/live/, data/paper/, ...):
    python scripts/halt.py --status --config config/live.yaml      # show halts + per-city drawdown
    python scripts/halt.py --reset --config config/live.yaml       # clear account + ALL city halts
    python scripts/halt.py --reset KMDW --config config/live.yaml  # clear one city's halt
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from weather_alpha.config import load_config   # noqa: E402
from weather_alpha.positions import Book        # noqa: E402


def _print_status(book: Book) -> None:
    acct_dd = book.account_hwm_cents - book.realized_pnl_cents
    print(f"account: halted={book.account_halted}  realized=${book.realized_pnl_cents / 100:+.2f}  "
          f"peak=${book.account_hwm_cents / 100:+.2f}  drawdown=${acct_dd / 100:.2f}")
    stations = sorted({p.station for p in book.positions.values() if p.station})
    if not stations:
        print("  (no per-city positions yet)")
    for s in stations:
        c = book.realized_for_station(s)
        h = book.city_hwm_cents.get(s, 0)
        flag = "  [HALTED]" if s in book.halted_stations else ""
        print(f"  {s}: realized=${c / 100:+.2f}  peak=${h / 100:+.2f}  drawdown=${(h - c) / 100:.2f}{flag}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Show or reset drawdown halts.")
    ap.add_argument("--reset", nargs="?", const="__ALL__", metavar="STATION",
                    help="clear a halt: no arg = account + ALL cities; STATION = just that city")
    ap.add_argument("--status", action="store_true", help="show halts + drawdown without changing state")
    ap.add_argument("--config", required=True,
                    help="REQUIRED — the running bot's config (e.g. config/live.yaml); halt "
                         "state is read from its positions.json so it MUST match the live bot")
    args = ap.parse_args(argv)

    cfg = load_config(args.config, require_live_creds=False)   # #1: reset halts even from a credless shell
    book = Book.load(cfg.paths.positions_snapshot)

    if args.reset is not None:
        station = None if args.reset == "__ALL__" else args.reset
        book.reset_halt(station)
        book.save(cfg.paths.positions_snapshot)
        print(f"RESET — cleared {'account + all city halts' if station is None else station + ' halt'}.")

    _print_status(book)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
