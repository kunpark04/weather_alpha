"""Append per-contract decisions + fills to data/live_log.parquet.

Mirrors the notebook §7 schema and extends it with production fields (mode,
target_contracts, fill_*, kelly diagnostics). Old rows tolerated via sort=False
concat — they get NaN for new columns.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class LogRow:
    run_utc: pd.Timestamp
    anchor_date: pd.Timestamp
    model: str
    mode: str                           # "paper" | "live"
    t_utc: pd.Timestamp
    model_median: int
    model_lo10: int
    model_hi90: int
    ticker: str
    bucket_spec: str
    subtitle: str
    status: str
    is_live: bool
    yes_bid: float
    yes_ask: float
    no_bid: float
    no_ask: float
    yes_mid: float
    no_mid: float
    last_price: float
    p_model: float
    p_market: float | None
    best_side: str | None
    target_contracts: int
    entry_price_cents: int | None
    net_ev_cents: float | None
    kelly_f: float | None
    scaled_kelly: float | None
    exposure_frac: float | None
    throttle: float | None
    fill_status: str                    # "filled" | "skipped" | "rejected" | "no_target"
    fill_price_cents: int | None
    fill_contracts: int
    fee_cents: int | None


def append_rows(path: Path, rows: list[LogRow]) -> int:
    """Append rows; returns the new total row count. Creates the file if missing."""
    if not rows:
        return _count(path)
    df_new = pd.DataFrame([asdict(r) for r in rows])
    if Path(path).exists():
        df_old = pd.read_parquet(path)
        df = pd.concat([df_old, df_new], ignore_index=True, sort=False)
    else:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        df = df_new
    df.to_parquet(path)
    logger.info("live_log: appended %d rows -> %s (total %d)", len(df_new), path, len(df))
    return len(df)


def _count(path: Path) -> int:
    if not Path(path).exists():
        return 0
    return len(pd.read_parquet(path))


def settled_pnl_summary(path: Path, mode: str | None = None) -> dict:
    """Aggregate realized PnL from the log (post-settlement reconciliation)."""
    if not Path(path).exists():
        return {"n": 0, "realized_pnl_cents": 0, "n_filled": 0}
    df = pd.read_parquet(path)
    if mode:
        df = df[df.get("mode") == mode]
    filled = df[df.get("fill_status") == "filled"] if "fill_status" in df.columns else df.iloc[0:0]
    return {
        "n":                  int(len(df)),
        "n_filled":           int(len(filled)),
        "realized_pnl_cents": int(filled["realized_pnl_cents"].sum()) if "realized_pnl_cents" in filled.columns else 0,
    }
