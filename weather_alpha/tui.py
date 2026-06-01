"""Textual TUI — the constantly-on terminal dashboard.

Five panels:
  Header     mode (PAPER/LIVE), anchor date, bankroll, next-event countdown
  Prediction PMF top-N rows + median / 80% CI / peak bucket
  Market     six Kalshi contracts with yes/no quotes, p_model, p_market, fill side
  Positions  open positions with PnL
  Log        tail of the in-memory ring buffer

Workers:
  scheduler  runs every 60s, dispatches refresh / anchor / intraday cycles
"""

from __future__ import annotations

import asyncio
import logging
from typing import Iterable

import pandas as pd
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from weather_alpha.config import Config
from weather_alpha.engine import CycleResult, _current_bankroll, refresh_data, run_cycle, verify_bankroll
from weather_alpha.kalshi import KalshiClient
from weather_alpha.log import get_ring_buffer
from weather_alpha.model import ModelArtifacts
from weather_alpha.positions import Book
from weather_alpha.scheduler import Action, Scheduler

logger = logging.getLogger(__name__)


class StatusBar(Static):
    mode = reactive("paper")
    anchor = reactive("--")
    bankroll = reactive(0.0)
    realized = reactive(0)
    next_event = reactive("--")
    last_action = reactive("idle")

    def render(self) -> Text:
        color = "red" if self.mode == "live" else "green"
        return Text.assemble(
            ("Weather Alpha  ", "bold"),
            (f"[{self.mode.upper()}]  ", color),
            (f"anchor={self.anchor}  ", "yellow"),
            (f"bankroll=${self.bankroll:.2f}  ", ""),
            (f"realized={self.realized/100:+.2f}$  ", "cyan"),
            (f"next={self.next_event}  ", "magenta"),
            (f"last={self.last_action}", "dim"),
        )


class WeatherAlphaApp(App):
    CSS = """
    Screen { layout: vertical; }
    #top { height: 3; }
    #middle { height: 1fr; }
    #bottom { height: 18; }
    DataTable { height: 1fr; }
    RichLog { height: 1fr; border: solid grey; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "force_refresh", "Refresh now"),
        ("a", "force_anchor", "Run anchor"),
        ("k", "kill_switch", "Kill switch"),
    ]

    def __init__(self, cfg: Config, art: ModelArtifacts | None, book: Book) -> None:
        super().__init__()
        self._cfg = cfg
        self._art = art
        self._book = book
        self._scheduler = Scheduler(cfg, cfg.paths.scheduler_state)
        self._kalshi: KalshiClient | None = None
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical():
            yield StatusBar(id="status")
            with Horizontal(id="middle"):
                with Vertical():
                    yield Static("Prediction", classes="panel-title")
                    yield DataTable(id="pred_table")
                with Vertical():
                    yield Static("Kalshi market", classes="panel-title")
                    yield DataTable(id="market_table")
            with Horizontal(id="bottom"):
                with Vertical():
                    yield Static("Positions", classes="panel-title")
                    yield DataTable(id="positions_table")
                with Vertical():
                    yield Static("Log", classes="panel-title")
                    yield RichLog(id="log", highlight=True, markup=True)
        yield Footer()

    # ---- lifecycle ------------------------------------------------------------

    async def on_mount(self) -> None:
        self.title = f"Weather Alpha — {self._cfg.station}"
        self._setup_tables()
        self._kalshi = KalshiClient(self._cfg.kalshi, authenticated=self._cfg.is_live())
        await self._kalshi.__aenter__()
        try:
            await verify_bankroll(self._cfg, self._book, self._kalshi)
        except Exception:
            logger.exception("LIVE bankroll verification failed at startup")

        self._refresh_status()
        self._drain_log()
        self.set_interval(1.0, self._tick_ui)            # UI redraws
        self.set_interval(60.0, self._tick_scheduler)    # scheduler decisions

    async def on_unmount(self) -> None:
        if self._kalshi is not None:
            await self._kalshi.close()

    # ---- UI ticks -------------------------------------------------------------

    def _tick_ui(self) -> None:
        self._refresh_status()
        self._drain_log()

    def _refresh_status(self) -> None:
        bar = self.query_one(StatusBar)
        bar.mode = self._cfg.mode
        bar.bankroll = _current_bankroll(self._cfg, self._book)
        bar.realized = self._book.realized_pnl_cents
        td = self._scheduler.next_anchor_in()
        bar.next_event = _fmt_delta(td)
        bar.last_action = self._scheduler.state.last_action.value

    def _drain_log(self) -> None:
        widget: RichLog = self.query_one("#log", RichLog)
        lines = get_ring_buffer().snapshot(40)
        widget.clear()
        for line in lines:
            widget.write(line)

    # ---- scheduler tick --------------------------------------------------------

    def _tick_scheduler(self) -> None:
        if self._busy:
            return
        action = self._scheduler.due()
        if action == Action.IDLE:
            return
        self._busy = True
        self._dispatch(action)

    @work(exclusive=True)
    async def _dispatch(self, action: Action) -> None:
        try:
            if action == Action.DATA_REFRESH:
                logger.info("scheduler -> DATA_REFRESH")
                await refresh_data(self._cfg)
            elif action in (Action.ANCHOR, Action.INTRADAY):
                logger.info("scheduler -> %s", action.value.upper())
                results = await run_cycle(self._cfg, self._art, self._book, self._kalshi,
                                          skip_refresh=(action == Action.INTRADAY))
                for result in results:
                    self._render_cycle(result)
            self._scheduler.record(action)
        except Exception:
            logger.exception("scheduler action %s failed", action.value)
        finally:
            self._busy = False

    # ---- key actions ----------------------------------------------------------

    def action_force_refresh(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._dispatch(Action.DATA_REFRESH)

    def action_force_anchor(self) -> None:
        if self._busy:
            return
        self._busy = True
        self._dispatch(Action.ANCHOR)

    def action_kill_switch(self) -> None:
        path = self._cfg.paths.kill_switch
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"tripped at {pd.Timestamp.now(tz='UTC').isoformat()}\n", encoding="utf-8")
        logger.warning("KILL SWITCH FILE WRITTEN at %s — execution will refuse new trades", path)

    # ---- table renders --------------------------------------------------------

    def _setup_tables(self) -> None:
        pred = self.query_one("#pred_table", DataTable)
        pred.cursor_type = "row"
        pred.add_columns("°F", "P", "")
        market = self.query_one("#market_table", DataTable)
        market.cursor_type = "row"
        market.add_columns("bucket", "yes_ask", "no_ask", "p_model", "p_mkt", "side", "size")
        pos = self.query_one("#positions_table", DataTable)
        pos.cursor_type = "row"
        pos.add_columns("ticker", "side", "qty", "avg¢", "anchor")

    def _render_cycle(self, r: CycleResult) -> None:
        # Prediction table — top 10 PMF rows
        pred_widget = self.query_one("#pred_table", DataTable)
        pred_widget.clear()
        top = r.prediction.pmf.sort_values(ascending=False).head(10)
        for f, p in top.items():
            bar = "█" * int(p * 30)
            pred_widget.add_row(f"{int(f)}", f"{p:.3f}", bar)

        # Market table
        mkt_widget = self.query_one("#market_table", DataTable)
        mkt_widget.clear()
        by_ticker = {t.ticker: t for t in r.strategy.targets}
        # We don't have the full contracts list here — pull from log via book? Instead,
        # render only strategy targets + a synthetic row for non-targeted.
        for t in r.strategy.targets:
            r_ = t.rationale
            mkt_widget.add_row(
                t.bucket_spec,
                f"{r_.get('p_market', 0):.3f}",          # placeholder — true ask shown in log
                "-",
                f"{r_.get('p_model', 0):.3f}",
                f"{r_.get('p_market', 0):.3f}",
                t.side.upper(),
                f"{t.target_contracts}",
            )

        # Positions table
        pos_widget = self.query_one("#positions_table", DataTable)
        pos_widget.clear()
        for key, p in self._book.positions.items():
            if p.settled:
                continue
            pos_widget.add_row(p.ticker, p.side, str(p.contracts),
                               f"{p.avg_cost_cents:.1f}", p.anchor_date)


def _fmt_delta(td: pd.Timedelta) -> str:
    seconds = max(0, int(td.total_seconds()))
    hours, rem = divmod(seconds, 3600)
    minutes, _ = divmod(rem, 60)
    return f"{hours:02d}h{minutes:02d}m"
