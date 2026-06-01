"""Async scheduler — decides when to run anchor cycles and intraday refreshes.

Pure logic. The TUI (or any host) calls `Scheduler.due(now)` once per tick and
gets back an enum-ish hint about what to run next. Keeping it pure makes it trivial
to unit-test and to drive deterministically from a backtest harness.

Cadence:
  - DATA_REFRESH: every `cfg.scheduler.data_refresh_minutes`
  - ANCHOR:       once per local-day at T_HOUR_LOCAL + grace_minutes
  - INTRADAY:     every `cfg.execution.intraday_refresh_minutes` after anchor
                  and before market close (~midnight ET)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import pandas as pd

from weather_alpha.config import Config, MarketCfg

logger = logging.getLogger(__name__)


class Action(str, Enum):
    IDLE = "idle"
    DATA_REFRESH = "data_refresh"
    ANCHOR = "anchor"
    INTRADAY = "intraday"


@dataclass
class SchedulerState:
    last_data_refresh: pd.Timestamp | None = None
    last_anchor_run_date: pd.Timestamp | None = None     # local-date normalized
    last_intraday_run: pd.Timestamp | None = None
    last_action: Action = Action.IDLE


class Scheduler:
    def __init__(self, cfg: Config, state_path: Path | str | None = None):
        self._cfg = cfg
        self._state_path = Path(state_path) if state_path else None
        self.state = SchedulerState()
        if self._state_path is not None:
            self._load()

    def now_local(self) -> pd.Timestamp:
        return pd.Timestamp.now(tz=self._cfg.local_tz)

    def due(self, now: pd.Timestamp | None = None) -> Action:
        """Return the action that should run *next* if any, given current time."""
        now = now or self.now_local()
        if now.tz is None:
            now = now.tz_localize(self._cfg.local_tz)
        today = now.normalize()

        anchor_due_at = today + pd.Timedelta(
            hours=self._cfg.model.anchor_hour_local,
            minutes=self._cfg.scheduler.anchor_grace_minutes,
        )

        # Priority 1: anchor (once per day, if past anchor time + grace).
        if now >= anchor_due_at and (self.state.last_anchor_run_date is None
                                      or self.state.last_anchor_run_date < today):
            return Action.ANCHOR

        # Priority 2: data refresh (only if it's stale).
        stale_minutes = self._cfg.scheduler.data_refresh_minutes
        if (self.state.last_data_refresh is None
                or (now - self.state.last_data_refresh) > pd.Timedelta(minutes=stale_minutes)):
            return Action.DATA_REFRESH

        # Priority 3: intraday refresh (after anchor, before market close).
        intraday_gap = pd.Timedelta(minutes=self._cfg.execution.intraday_refresh_minutes)
        market_close_local = today + pd.Timedelta(hours=23)   # ~midnight ET ≈ 11pm CT
        if (self.state.last_anchor_run_date == today and now < market_close_local
                and (self.state.last_intraday_run is None
                     or (now - self.state.last_intraday_run) > intraday_gap)):
            return Action.INTRADAY

        return Action.IDLE

    def record(self, action: Action, when: pd.Timestamp | None = None) -> None:
        when = when or self.now_local()
        self.state.last_action = action
        if action == Action.DATA_REFRESH:
            self.state.last_data_refresh = when
        elif action == Action.ANCHOR:
            self.state.last_anchor_run_date = when.normalize()
            self.state.last_intraday_run = when
        elif action == Action.INTRADAY:
            self.state.last_intraday_run = when
        if self._state_path is not None:
            self._save()

    # ---- persistence (W2: survive a restart without re-firing the day's anchor) ----

    def _save(self) -> None:
        """Atomically persist scheduler state. Best-effort: a write failure logs but
        never propagates into the trading loop."""
        s = self.state
        def _iso(t: pd.Timestamp | None) -> str | None:
            return t.isoformat() if t is not None else None
        data = {
            "last_data_refresh":    _iso(s.last_data_refresh),
            "last_anchor_run_date": _iso(s.last_anchor_run_date),
            "last_intraday_run":    _iso(s.last_intraday_run),
            "last_action":          s.last_action.value,
        }
        try:
            path = self._state_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("could not persist scheduler state to %s", self._state_path)

    def _load(self) -> None:
        path = self._state_path
        if not path.exists():
            return
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("could not read scheduler state at %s; starting fresh", path)
            return
        def _ts(v) -> pd.Timestamp | None:
            return pd.Timestamp(v) if v else None
        self.state = SchedulerState(
            last_data_refresh=_ts(d.get("last_data_refresh")),
            last_anchor_run_date=_ts(d.get("last_anchor_run_date")),
            last_intraday_run=_ts(d.get("last_intraday_run")),
            last_action=Action(d.get("last_action", Action.IDLE.value)),
        )
        logger.info("scheduler state restored from %s (last_anchor_run_date=%s, last_action=%s)",
                    path.name, self.state.last_anchor_run_date, self.state.last_action.value)

    def next_anchor_in(self) -> pd.Timedelta:
        """How long until the next anchor (for display)."""
        now = self.now_local()
        today = now.normalize()
        anchor_today = today + pd.Timedelta(
            hours=self._cfg.model.anchor_hour_local,
            minutes=self._cfg.scheduler.anchor_grace_minutes,
        )
        if now < anchor_today and self.state.last_anchor_run_date != today:
            return anchor_today - now
        # Else: tomorrow's anchor.
        return (today + pd.Timedelta(days=1)
                + pd.Timedelta(hours=self._cfg.model.anchor_hour_local,
                                minutes=self._cfg.scheduler.anchor_grace_minutes)) - now


class MarketAnchorScheduler:
    """Per-market anchor scheduler for the resident, model-free, multi-timezone bot.

    Fires each market at ITS OWN local 1 PM (+ grace), once per local day, regardless of
    timezone — so a single resident process serves cities in ANY US tz (Eastern, Central,
    Mountain, Pacific, the no-DST zones, …). Persists the per-market last-run date so a
    restart never re-fires a city already done today. (The model path keeps the single-tz
    `Scheduler` above, which also does data-refresh + intraday.)
    """

    def __init__(self, cfg: Config, state_path: Path | str | None = None):
        self._cfg = cfg
        self._state_path = Path(state_path) if state_path else None
        self.last_anchor: dict[str, str] = {}      # market name -> ISO local date last fired
        if self._state_path is not None:
            self._load()

    def _anchor_for(self, m: MarketCfg, now_utc: pd.Timestamp) -> tuple[pd.Timestamp, pd.Timestamp, str]:
        """(now in the market's tz, today's anchor instant in that tz, today's ISO date)."""
        now_local = now_utc.tz_convert(m.local_tz)
        today = now_local.normalize()
        anchor_at = today + pd.Timedelta(hours=self._cfg.model.anchor_hour_local,
                                         minutes=self._cfg.scheduler.anchor_grace_minutes)
        return now_local, anchor_at, str(today.date())

    def due_markets(self, markets: list[MarketCfg], now_utc: pd.Timestamp | None = None) -> list[MarketCfg]:
        """Markets whose local anchor has passed today and that haven't fired today yet."""
        now_utc = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
        due = []
        for m in markets:
            now_local, anchor_at, today = self._anchor_for(m, now_utc)
            if now_local >= anchor_at and self.last_anchor.get(m.name) != today:
                due.append(m)
        return due

    def seconds_until_next(self, markets: list[MarketCfg], now_utc: pd.Timestamp | None = None) -> float:
        """Seconds until the soonest market anchor still needing to fire (for sleeping)."""
        now_utc = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
        best: float | None = None
        for m in markets:
            now_local, anchor_at, today = self._anchor_for(m, now_utc)
            if now_local < anchor_at and self.last_anchor.get(m.name) != today:
                nxt = anchor_at                                    # today's, not yet fired
            else:
                tomorrow = now_local.normalize() + pd.Timedelta(days=1)
                nxt = tomorrow + pd.Timedelta(hours=self._cfg.model.anchor_hour_local,
                                              minutes=self._cfg.scheduler.anchor_grace_minutes)
            secs = (nxt - now_local).total_seconds()
            best = secs if best is None else min(best, secs)
        return max(0.0, best if best is not None else 60.0)

    def record(self, market: MarketCfg, now_utc: pd.Timestamp | None = None) -> None:
        now_utc = now_utc if now_utc is not None else pd.Timestamp.now(tz="UTC")
        today = now_utc.tz_convert(market.local_tz).normalize()
        self.last_anchor[market.name] = str(today.date())
        if self._state_path is not None:
            self._save()

    def _save(self) -> None:
        try:
            path = self._state_path
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps({"last_anchor": self.last_anchor}, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            logger.warning("could not persist market scheduler state to %s", self._state_path)

    def _load(self) -> None:
        path = self._state_path
        if not path.exists():
            return
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            self.last_anchor = {str(k): str(v) for k, v in dict(d.get("last_anchor", {})).items()}
        except (OSError, json.JSONDecodeError):
            logger.warning("could not read market scheduler state at %s; starting fresh", path)
