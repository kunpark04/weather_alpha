"""Concise operator-facing terminal stream: one line per decision / fill / settle / halt.

This is the human narration the user watches (foreground process or `journalctl -f`),
deliberately SEPARATE from the structured debug logger. Conventions: one line per event,
prices in ¢, money in $, no multi-line dumps. Designed for the headless `--loop` bot.
"""

from __future__ import annotations

import sys


def _ensure_utf8() -> None:
    """Emoji / ¢ / °F are UTF-8; a cp1252 Windows console would crash on write. The Linux
    deploy is already UTF-8, but this keeps dev runs on Windows from blowing up."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


_ensure_utf8()


def report(msg: str) -> None:
    """Emit one operator-facing line to stdout (flushed so it appears immediately)."""
    print(msg, file=sys.stdout, flush=True)


def usd(cents: float) -> str:
    return f"${cents / 100:,.2f}"


def usd_signed(cents: float) -> str:
    return f"{'+' if cents >= 0 else '-'}${abs(cents) / 100:,.2f}"
