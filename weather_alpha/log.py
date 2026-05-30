"""Structured logging. Writes to rotating file + an in-memory ring buffer the TUI consumes."""

from __future__ import annotations

import logging
import logging.handlers
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Deque

_FMT = "%(asctime)s %(levelname)s %(name)s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


class RingBufferHandler(logging.Handler):
    """Holds the last N formatted log lines for the TUI panel."""

    def __init__(self, capacity: int = 500) -> None:
        super().__init__()
        self._buf: Deque[str] = deque(maxlen=capacity)
        self._lock = Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            self.handleError(record)
            return
        with self._lock:
            self._buf.append(line)

    def snapshot(self, n: int | None = None) -> list[str]:
        with self._lock:
            items = list(self._buf)
        return items[-n:] if n else items


_ring = RingBufferHandler()


def get_ring_buffer() -> RingBufferHandler:
    return _ring


def setup_logging(logs_dir: Path, level: int = logging.INFO) -> None:
    """Idempotent. Call once at startup."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    if getattr(root, "_fa_configured", False):
        return
    root.setLevel(level)

    fmt = logging.Formatter(_FMT, datefmt=_DATEFMT)

    fh = logging.handlers.RotatingFileHandler(
        logs_dir / "weather_alpha.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    _ring.setFormatter(fmt)
    root.addHandler(_ring)

    # Quiet noisy libraries.
    for noisy in ("httpx", "httpcore", "urllib3", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    root._fa_configured = True  # type: ignore[attr-defined]
