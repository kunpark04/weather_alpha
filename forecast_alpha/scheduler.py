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

import logging
from dataclasses import dataclass
from enum import Enum

import pandas as pd

from forecast_alpha.config import Config

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
    def __init__(self, cfg: Config):
        self._cfg = cfg
        self.state = SchedulerState()

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
