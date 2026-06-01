"""Multi-market refactor tests — config back-compat + validation, per-station settlement,
drawdown circuit-breakers, the per-timezone scheduler, and the run_cycle market loop.

Async paths are driven via asyncio.run() inside sync tests, so no pytest-asyncio config is
needed.  Run:  pytest tests/test_multimarket.py     (or: python tests/test_multimarket.py)
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from weather_alpha import engine                                            # noqa: E402
from weather_alpha.config import load_config                               # noqa: E402
from weather_alpha.execution import ExecutionResult, reconcile_settlements  # noqa: E402
from weather_alpha.positions import Book, Position                          # noqa: E402
from weather_alpha.scheduler import MarketAnchorScheduler                   # noqa: E402
from weather_alpha.strategy import StrategyOutput                           # noqa: E402

_CHI = {"name": "CHI", "event_pattern": "KXHIGHCHI", "station": "KMDW", "local_tz": "America/Chicago"}
_NYC = {"name": "NYC", "event_pattern": "KXHIGHNY",  "station": "KNYC", "local_tz": "America/New_York"}
_LAX = {"name": "LAX", "event_pattern": "KXHIGHLAX", "station": "KLAX", "local_tz": "America/Los_Angeles"}


def _write_cfg(markets, mode="paper") -> str:
    """Build a temp multi-market config from the houston_paper base with isolated temp paths."""
    raw = yaml.safe_load((_ROOT / "config" / "houston_paper.yaml").read_text(encoding="utf-8"))
    raw["mode"] = mode
    raw.pop("station", None); raw.pop("local_tz", None)
    raw["execution"].pop("market_event_pattern", None)
    raw["markets"] = markets
    d = tempfile.mkdtemp()
    nb = raw["paths"]["notebook_v3"]
    raw["paths"] = {"data_dir": d, "model_dir": d, "live_log": os.path.join(d, "l.parquet"),
                    "positions_snapshot": os.path.join(d, "pos.json"), "notebook_v3": nb,
                    "logs_dir": os.path.join(d, "logs"), "kill_switch": os.path.join(d, "KILL"),
                    "scheduler_state": os.path.join(d, "s.json")}
    f = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    yaml.safe_dump(raw, f); f.close()
    return f.name


class _MockKalshi:
    def __init__(self, by_pattern=None):
        self._by = by_pattern or {}

    async def fetch_event(self, anchor, pattern):
        return list(self._by.get(pattern, []))


# --- config: back-compat + multi-market + validation -----------------------

def test_single_market_backcompat():
    c = load_config(str(_ROOT / "config" / "houston_paper.yaml"))
    assert len(c.markets) == 1
    m = c.markets[0]
    assert (m.event_pattern, m.station, m.local_tz) == (c.execution.market_event_pattern, c.station, c.local_tz)


def test_multi_market_mixed_tz_loads():
    c = load_config(_write_cfg([_CHI, _NYC]))
    assert [m.name for m in c.markets] == ["CHI", "NYC"]
    assert {m.local_tz for m in c.markets} == {"America/Chicago", "America/New_York"}
    assert c.station == "KMDW" and c.execution.market_event_pattern == "KXHIGHCHI"   # back-filled


def test_validation_rejects_bad_tz_and_dups():
    for mutate in (
        lambda ms: ms.__setitem__(1, {**_NYC, "local_tz": "America/Nowhere"}),
        lambda ms: ms.__setitem__(1, {**_NYC, "name": "CHI"}),
        lambda ms: ms.__setitem__(1, {**_NYC, "event_pattern": "KXHIGHCHI"}),
    ):
        ms = [dict(_CHI), dict(_NYC)]; mutate(ms)
        try:
            load_config(_write_cfg(ms))
            raise AssertionError("expected ValueError")
        except ValueError:
            pass


# --- per-station settlement + shared book ----------------------------------

def test_per_station_settlement_no_contamination():
    b = Book()
    b.add_fill("KXHIGHCHI-26MAY30-B90", "yes", 3, 45, 1, "90-91", "x", "2026-05-30", station="KMDW")
    b.add_fill("KXHIGHTHOU-26MAY30-B92", "yes", 3, 45, 1, "92-93", "x", "2026-05-30", station="KHOU")
    cli = pd.DataFrame({"date": ["2026-05-30"], "max_temp_f": [91]})
    assert reconcile_settlements(b, cli, station="KMDW") > 0                  # 90-91 wins at 91
    assert b.open_for("KXHIGHCHI-26MAY30-B90", "yes").settled
    assert not b.open_for("KXHIGHTHOU-26MAY30-B92", "yes").settled            # NOT vs KMDW's high
    assert reconcile_settlements(b, cli, station="KHOU") < 0                  # 92-93 loses at 91
    assert b.open_for("KXHIGHTHOU-26MAY30-B92", "yes").settled


def test_shared_book_no_ticker_collision():
    b = Book()
    b.add_fill("KXHIGHCHI-X-A", "yes", 1, 50, 0, "70-71", "x", "2026-05-30", station="KMDW")
    b.add_fill("KXHIGHNY-X-A", "yes", 1, 50, 0, "70-71", "x", "2026-05-30", station="KNYC")
    assert len(b.positions) == 2
    assert b.open_for("KXHIGHCHI-X-A", "yes").station == "KMDW"
    assert b.open_for("KXHIGHNY-X-A", "yes").station == "KNYC"


def test_legacy_backfill():
    b = Book(); b.add_fill("T", "yes", 1, 50, 0, "70-71", "x", "2026-05-30")
    assert b.open_for("T", "yes").station == ""
    assert b.backfill_station("KMDW") == 1
    assert b.open_for("T", "yes").station == "KMDW"


# --- drawdown circuit-breakers ---------------------------------------------

def _settle(book, station, realized, i):
    k = f"{station}-{i}:yes"
    book.positions[k] = Position(ticker=k.split(":")[0], side="yes", contracts=1, avg_cost_cents=50,
                                 total_fees_cents=0, bucket_spec="70-71", opened_utc="x",
                                 anchor_date="2026-05-20", station=station, settled=True,
                                 realized_pnl_cents=realized)
    book.realized_pnl_cents += realized


def test_per_city_and_account_drawdown_latch():
    b = Book(); b.bankroll_cents = 2500                  # $25 -> city stop $6.25, account stop $12.50
    _settle(b, "KMDW", 200, 0)
    assert b.update_drawdown_halts(2500, 0.25, 0.50) == [] and not b.is_halted("KMDW")
    _settle(b, "KMDW", -900, 1)                          # peak +2.00, now -7.00 -> dd $9 >= $6.25
    assert b.update_drawdown_halts(2500, 0.25, 0.50) and b.is_halted("KMDW") and not b.account_halted
    _settle(b, "KHOU", -500, 2)                          # account dd $14 >= $12.50
    assert b.update_drawdown_halts(2500, 0.25, 0.50) and b.account_halted and b.is_halted("KHOU")


def test_reset_clears_without_retrip():
    b = Book(); b.bankroll_cents = 2500
    _settle(b, "KMDW", 200, 0); _settle(b, "KMDW", -900, 1)
    b.update_drawdown_halts(2500, 0.25, 0.50)
    assert b.is_halted("KMDW")
    b.reset_halt("KMDW")
    assert not b.is_halted("KMDW")
    assert b.update_drawdown_halts(2500, 0.25, 0.50) == []        # peak reset -> no immediate re-latch


def test_drawdown_state_round_trips_json():
    b = Book(); b.bankroll_cents = 2500
    _settle(b, "KMDW", -900, 0); b.update_drawdown_halts(2500, 0.25, 0.50)
    b2 = Book.from_json(b.to_json())
    assert b2.halted_stations == b.halted_stations and b2.city_hwm_cents == b.city_hwm_cents
    assert b2.account_halted == b.account_halted and b2.account_hwm_cents == b.account_hwm_cents


# --- per-timezone scheduler ------------------------------------------------

def test_scheduler_fires_each_tz_at_its_own_1pm():
    cfg = load_config(_write_cfg([_CHI, _NYC, _LAX]))
    s = MarketAnchorScheduler(cfg)
    names = lambda ms: sorted(m.name for m in ms)
    t = lambda h: pd.Timestamp(f"2026-06-15 {h:02d}:00", tz="UTC")           # summer / DST
    assert names(s.due_markets(cfg.markets, t(17))) == ["NYC"]               # 1pm EDT
    assert names(s.due_markets(cfg.markets, t(18))) == ["CHI", "NYC"]        # +1pm CDT
    assert names(s.due_markets(cfg.markets, t(20))) == ["CHI", "LAX", "NYC"]  # +1pm PDT


def test_scheduler_record_and_dst():
    cfg = load_config(_write_cfg([_CHI, _NYC, _LAX]))
    s = MarketAnchorScheduler(cfg)
    nyc = next(m for m in cfg.markets if m.name == "NYC")
    s.record(nyc, pd.Timestamp("2026-06-15 17:00", tz="UTC"))
    due18 = s.due_markets(cfg.markets, pd.Timestamp("2026-06-15 18:00", tz="UTC"))
    assert [m.name for m in due18] == ["CHI"]                                # NYC already fired today
    winter = MarketAnchorScheduler(cfg).due_markets(cfg.markets, pd.Timestamp("2026-01-15 18:00", tz="UTC"))
    assert [m.name for m in winter] == ["NYC"]                               # 1pm EST = 18:00 UTC


# --- run_cycle market loop -------------------------------------------------

def test_run_cycle_loops_markets_per_market_results():
    cfg = load_config(_write_cfg([_CHI, _NYC]))
    results = asyncio.run(engine.run_cycle(cfg, None, Book(), _MockKalshi()))
    assert [r.diagnostics["market"] for r in results] == ["CHI", "NYC"]
    assert all(r.execution.fills == 0 for r in results)                      # no contracts -> event guard


def test_run_cycle_honors_halt():
    cfg = load_config(_write_cfg([_CHI]))
    book = Book(); book.halted_stations = ["KMDW"]
    results = asyncio.run(engine.run_cycle(cfg, None, book, _MockKalshi()))
    assert results[0].strategy.diagnostics.get("halted") and results[0].execution.fills == 0


# --- reporter smoke --------------------------------------------------------

def test_reporter_smoke():
    engine._report_decision("[13:00 CHI]", StrategyOutput(targets=[], diagnostics={"sum_asks": 0.93}))
    engine._report_fills("[13:00 CHI]", ExecutionResult(
        fills=2, skipped=0, diagnostics={},
        realized_orders=[{"contracts": 3, "fill_price_cents": 47, "fee_cents": 2}]))


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"\n{len(fns)} tests passed")
