"""In-memory position state + JSON snapshot persistence.

Positions are keyed by (ticker, side). Each position tracks contracts, average cost in
cents, and cumulative fees paid. On settlement (when CLI truth drops for the anchor
date) the position can be marked realized.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Position:
    ticker: str
    side: str                             # "yes" | "no"
    contracts: int
    avg_cost_cents: float                 # weighted entry cost per contract
    total_fees_cents: int
    bucket_spec: str
    opened_utc: str                       # ISO timestamp string
    anchor_date: str                      # ISO date string
    settled: bool = False
    realized_pnl_cents: int = 0
    settled_utc: str | None = None


@dataclass
class Book:
    positions: dict[str, Position] = field(default_factory=dict)        # key = f"{ticker}:{side}"
    realized_pnl_cents: int = 0
    fees_paid_cents: int = 0
    daily_loss_cents: dict[str, int] = field(default_factory=dict)      # date_iso -> cents

    @staticmethod
    def _key(ticker: str, side: str) -> str:
        return f"{ticker}:{side}"

    def add_fill(self, ticker: str, side: str, contracts: int, fill_cents: int,
                 fee_cents: int, bucket_spec: str, opened_utc: str, anchor_date: str) -> None:
        key = self._key(ticker, side)
        if key in self.positions:
            p = self.positions[key]
            total_cost = p.contracts * p.avg_cost_cents + contracts * fill_cents
            p.contracts += contracts
            p.avg_cost_cents = total_cost / max(p.contracts, 1)
            p.total_fees_cents += fee_cents
        else:
            self.positions[key] = Position(
                ticker=ticker,
                side=side,
                contracts=contracts,
                avg_cost_cents=fill_cents,
                total_fees_cents=fee_cents,
                bucket_spec=bucket_spec,
                opened_utc=opened_utc,
                anchor_date=anchor_date,
            )
        self.fees_paid_cents += fee_cents

    def settle(self, ticker: str, side: str, won: bool, settled_utc: str) -> int:
        """Resolve a position. Returns realized PnL in cents."""
        key = self._key(ticker, side)
        if key not in self.positions:
            return 0
        p = self.positions[key]
        if p.settled:
            return p.realized_pnl_cents
        gross_per = 100 - p.avg_cost_cents if won else -p.avg_cost_cents
        realized = int(round(gross_per * p.contracts - p.total_fees_cents))
        p.settled = True
        p.realized_pnl_cents = realized
        p.settled_utc = settled_utc
        self.realized_pnl_cents += realized
        self.daily_loss_cents[p.anchor_date] = (
            self.daily_loss_cents.get(p.anchor_date, 0) + min(0, realized)
        )
        return realized

    def exposure_cents(self) -> int:
        """Total cents tied up in open positions (sum of contracts * avg_cost)."""
        return sum(int(p.contracts * p.avg_cost_cents) for p in self.positions.values() if not p.settled)

    def open_for(self, ticker: str, side: str) -> Position | None:
        return self.positions.get(self._key(ticker, side))

    # ---- persistence -----------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps({
            "positions":          {k: asdict(v) for k, v in self.positions.items()},
            "realized_pnl_cents": self.realized_pnl_cents,
            "fees_paid_cents":    self.fees_paid_cents,
            "daily_loss_cents":   self.daily_loss_cents,
        }, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Book":
        d = json.loads(raw)
        positions = {k: Position(**v) for k, v in d.get("positions", {}).items()}
        return cls(
            positions=positions,
            realized_pnl_cents=int(d.get("realized_pnl_cents", 0)),
            fees_paid_cents=int(d.get("fees_paid_cents", 0)),
            daily_loss_cents=dict(d.get("daily_loss_cents", {})),
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> "Book":
        path = Path(path)
        if not path.exists():
            return cls()
        return cls.from_json(path.read_text(encoding="utf-8"))
