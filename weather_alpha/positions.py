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
    station: str = ""                     # NWS settlement station (e.g. KMDW); "" = legacy/pre-multimarket
    settled: bool = False
    realized_pnl_cents: int = 0
    settled_utc: str | None = None


@dataclass
class Book:
    positions: dict[str, Position] = field(default_factory=dict)        # key = f"{ticker}:{side}"
    realized_pnl_cents: int = 0
    fees_paid_cents: int = 0
    daily_loss_cents: dict[str, int] = field(default_factory=dict)      # date_iso -> cents
    bankroll_cents: int | None = None     # authoritative running bankroll. LIVE: verified from
                                          # Kalshi get_balance() at activation + re-synced after
                                          # settlement. None => PAPER computes it from realized PnL.
    # --- Drawdown circuit-breakers (latched until manual reset) --------------------
    city_hwm_cents: dict[str, int] = field(default_factory=dict)    # station -> peak cumulative realized
    account_hwm_cents: int = 0                                      # peak cumulative account realized
    halted_stations: list[str] = field(default_factory=list)       # per-city halts (until reset_halt)
    account_halted: bool = False                                    # whole-account halt (until reset_halt)

    @staticmethod
    def _key(ticker: str, side: str) -> str:
        return f"{ticker}:{side}"

    def add_fill(self, ticker: str, side: str, contracts: int, fill_cents: int,
                 fee_cents: int, bucket_spec: str, opened_utc: str, anchor_date: str,
                 station: str = "") -> None:
        key = self._key(ticker, side)
        if key in self.positions:
            p = self.positions[key]
            total_cost = p.contracts * p.avg_cost_cents + contracts * fill_cents
            p.contracts += contracts
            p.avg_cost_cents = total_cost / max(p.contracts, 1)
            p.total_fees_cents += fee_cents
            if not p.station and station:
                p.station = station
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
                station=station,
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

    def daily_outlay_cents(self, anchor_date: str) -> int:
        """Cents staked today for one anchor date = worst-case loss at risk before
        settlement. Used by the intraday circuit breaker (C3): daily_loss_cents only
        accrues at next-day settlement, so stake-at-risk is the honest same-day proxy."""
        return sum(int(p.contracts * p.avg_cost_cents)
                   for p in self.positions.values() if p.anchor_date == anchor_date)

    def open_for(self, ticker: str, side: str) -> Position | None:
        return self.positions.get(self._key(ticker, side))

    # ---- drawdown circuit-breakers ---------------------------------------------

    def realized_for_station(self, station: str) -> int:
        return sum(p.realized_pnl_cents for p in self.positions.values()
                   if p.settled and p.station == station)

    def is_halted(self, station: str) -> bool:
        """A city may not open new trades if the whole account is halted or it is halted."""
        return self.account_halted or station in self.halted_stations

    def reset_halt(self, station: str | None = None) -> None:
        """Clear a latched halt. station=None clears the account halt + every city halt.
        Resets the relevant high-water mark(s) to the current cumulative realized, so a
        stale peak doesn't immediately re-trip the halt on the next update."""
        if station is None:
            self.account_halted = False
            self.halted_stations.clear()
            self.account_hwm_cents = self.realized_pnl_cents
            self.city_hwm_cents = {s: self.realized_for_station(s)
                                   for s in {p.station for p in self.positions.values() if p.station}}
        elif station in self.halted_stations:
            self.halted_stations.remove(station)
            self.city_hwm_cents[station] = self.realized_for_station(station)

    def update_drawdown_halts(self, balance_cents: int, per_city_pct: float,
                              account_pct: float) -> list[str]:
        """Refresh the peak (HWM) of cumulative realized P&L and LATCH a halt when drawdown
        (peak − current) reaches the configured fraction of the RUNNING account balance:
        per-city at per_city_pct (e.g. 25%), whole-account at account_pct (e.g. 50%). The
        threshold scales with the account (tightens as it bleeds). Latched until reset_halt().
        Returns human-readable labels of any NEWLY-latched halt (for the terminal reporter)."""
        newly: list[str] = []
        if balance_cents <= 0:
            return newly
        acct = self.realized_pnl_cents
        self.account_hwm_cents = max(self.account_hwm_cents, acct)
        if not self.account_halted and (self.account_hwm_cents - acct) >= account_pct * balance_cents:
            self.account_halted = True
            newly.append(f"ACCOUNT drawdown ${(self.account_hwm_cents - acct) / 100:.2f} "
                         f">= {account_pct:.0%} of ${balance_cents / 100:.2f}")
        for s in {p.station for p in self.positions.values() if p.station}:
            c = self.realized_for_station(s)
            self.city_hwm_cents[s] = max(self.city_hwm_cents.get(s, 0), c)
            if s not in self.halted_stations and (self.city_hwm_cents[s] - c) >= per_city_pct * balance_cents:
                self.halted_stations.append(s)
                newly.append(f"{s} drawdown ${(self.city_hwm_cents[s] - c) / 100:.2f} "
                             f">= {per_city_pct:.0%} of ${balance_cents / 100:.2f}")
        return newly

    def backfill_station(self, default_station: str) -> int:
        """Assign a station to any legacy position that predates multi-market (station=="").
        Such positions only exist in a single-market book, so they belong to that one city
        (== the config's first/only market). Returns the number updated."""
        n = 0
        for p in self.positions.values():
            if not p.station:
                p.station = default_station
                n += 1
        return n

    # ---- persistence -----------------------------------------------------------

    def to_json(self) -> str:
        return json.dumps({
            "positions":          {k: asdict(v) for k, v in self.positions.items()},
            "realized_pnl_cents": self.realized_pnl_cents,
            "fees_paid_cents":    self.fees_paid_cents,
            "daily_loss_cents":   self.daily_loss_cents,
            "bankroll_cents":     self.bankroll_cents,
            "city_hwm_cents":     self.city_hwm_cents,
            "account_hwm_cents":  self.account_hwm_cents,
            "halted_stations":    self.halted_stations,
            "account_halted":     self.account_halted,
        }, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "Book":
        d = json.loads(raw)
        positions = {k: Position(**v) for k, v in d.get("positions", {}).items()}
        bankroll = d.get("bankroll_cents")
        return cls(
            positions=positions,
            realized_pnl_cents=int(d.get("realized_pnl_cents", 0)),
            fees_paid_cents=int(d.get("fees_paid_cents", 0)),
            daily_loss_cents=dict(d.get("daily_loss_cents", {})),
            bankroll_cents=(int(bankroll) if bankroll is not None else None),
            city_hwm_cents={str(k): int(v) for k, v in d.get("city_hwm_cents", {}).items()},
            account_hwm_cents=int(d.get("account_hwm_cents", 0)),
            halted_stations=list(d.get("halted_stations", [])),
            account_halted=bool(d.get("account_halted", False)),
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
