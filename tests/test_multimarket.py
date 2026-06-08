"""Multi-market refactor tests — config back-compat + validation, per-station settlement,
drawdown circuit-breakers, the per-timezone scheduler, and the run_cycle market loop.

Async paths are driven via asyncio.run() inside sync tests, so no pytest-asyncio config is
needed.  Run:  pytest tests/test_multimarket.py     (or: python tests/test_multimarket.py)
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
import tempfile
from pathlib import Path

import httpx
import pandas as pd
import yaml

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from weather_alpha import engine                                            # noqa: E402
from weather_alpha.config import load_config, KalshiCfg                    # noqa: E402
from weather_alpha.execution import (                                       # noqa: E402
    ExecutionResult, KillSwitchTripped, execute, reconcile_settlements)
from weather_alpha.kalshi import _spec_from_market, KalshiContract, KalshiClient, event_date_code, KalshiPosition  # noqa: E402
from weather_alpha.positions import Book, Position, RunBudget               # noqa: E402
from weather_alpha.scheduler import MarketAnchorScheduler                   # noqa: E402
from weather_alpha.strategy import StrategyOutput, TargetPosition           # noqa: E402

_CHI = {"name": "CHI", "event_pattern": "KXHIGHCHI", "station": "KMDW", "local_tz": "America/Chicago"}
_NYC = {"name": "NYC", "event_pattern": "KXHIGHNY",  "station": "KNYC", "local_tz": "America/New_York"}
_LAX = {"name": "LAX", "event_pattern": "KXHIGHLAX", "station": "KLAX", "local_tz": "America/Los_Angeles"}


def _write_cfg(markets, mode="paper", per_anchor=6) -> str:
    """Build a temp multi-market config from the houston_paper base with isolated temp paths."""
    raw = yaml.safe_load((_ROOT / "config" / "houston_paper.yaml").read_text(encoding="utf-8"))
    raw["mode"] = mode
    raw["risk"]["per_anchor_max_trades"] = per_anchor
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


class _ThrowingKalshi:
    """fetch_event raises for one pattern — exercises run_cycle's per-market isolation (#2)."""
    def __init__(self, throw_pattern):
        self._throw = throw_pattern

    async def fetch_event(self, anchor, pattern):
        if pattern == self._throw:
            raise RuntimeError(f"simulated {pattern} fetch failure")
        return []


class _MockLiveClient:
    """Minimal authenticated client for LIVE-path execute() tests (#3/#4/#6/#10/W3).

    `fills` maps ticker -> qty that fills per placed order; set it BELOW the target to model a
    partial fill (the resting remainder is then expected to be cancelled by execute()). `ack_fee`
    optionally injects an exchange-reported fee onto the CreateOrderResponse (W4)."""
    def __init__(self, held=None, fills=None, ack_fee=None):
        self._held = dict(held or {})        # (ticker, side) -> (contracts, avg_cents) on the "exchange"
        self._fills = dict(fills or {})       # ticker -> qty that fills when an order is placed
        self._ack_fee = ack_fee               # dict(ticker -> fee_dollars) for W4, else None
        self.placed: list = []                # (ticker, side, count) for BUY orders
        self.flatten_sells: list = []         # (ticker, side, count) for SELL (flatten) orders
        self.coids: list = []                 # client_order_id per placed order (W3 uniqueness)
        self.cancelled: list = []

    async def get_positions(self):
        return [KalshiPosition(ticker=t, side=s, contracts=c, avg_price_cents=a)
                for (t, s), (c, a) in self._held.items() if c]

    async def place_order(self, *, ticker, side, action, count, limit_price_cents, client_order_id=None):
        if action == "sell":
            self.flatten_sells.append((ticker, side, count))
            return {"order": {"order_id": f"flat-{ticker}"}}
        self.placed.append((ticker, side, count))
        self.coids.append(client_order_id)
        q = self._fills.get(ticker, 0)
        if q:
            c0, a0 = self._held.get((ticker, side), (0, 0))
            c1 = c0 + q
            self._held[(ticker, side)] = (c1, int(round((c0 * a0 + q * limit_price_cents) / c1)))
        ack = {"order": {"order_id": f"oid-{ticker}", "fill_count_fp": f"{q:.2f}"}}
        if self._ack_fee is not None and ticker in self._ack_fee:
            ack["order"]["taker_fees_dollars"] = f"{self._ack_fee[ticker]:.4f}"
            ack["order"]["maker_fees_dollars"] = "0.0000"
        return ack

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return {"ok": True}


def _live_contract(ticker):
    return KalshiContract(ticker=ticker, event_ticker="KXHIGHCHI-26JUN01", bucket_spec="72-73",
                          subtitle="72° to 73°", strike_type="between", strike=72, yes_bid=0.49,
                          yes_ask=0.50, no_bid=0.49, no_ask=0.50, last_price=0.50, volume_24h=None,
                          open_interest=None, open_time_utc=None, close_time_utc=None,
                          status="active", is_live=True)


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

def test_scheduler_fires_each_tz_within_its_anchor_window():
    # #8: each city is due only WITHIN the post-anchor window, so a check well past one tz's 1 PM
    # ages it out (no off-anchor catch-up). Summer/DST: 1 PM local = NYC 17z, CHI 18z, LAX 20z.
    cfg = load_config(_write_cfg([_CHI, _NYC, _LAX]))
    s = MarketAnchorScheduler(cfg)
    names = lambda ms: sorted(m.name for m in ms)
    assert names(s.due_markets(cfg.markets, pd.Timestamp("2026-06-15 17:05", tz="UTC"))) == ["NYC"]
    assert names(s.due_markets(cfg.markets, pd.Timestamp("2026-06-15 18:05", tz="UTC"))) == ["CHI"]  # NYC aged out (+65m)
    assert names(s.due_markets(cfg.markets, pd.Timestamp("2026-06-15 20:05", tz="UTC"))) == ["LAX"]  # CHI/NYC aged out


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


def test_scheduler_stagger_default_off_and_paper_config_on():
    # The knob defaults to OFF (legacy / single-market no-op) when a config omits it...
    assert load_config(_write_cfg([_CHI])).scheduler.inter_market_stagger_seconds == 0.0
    # ...and the deployed 20-city PAPER bot turns it on, so the 429 fix is actually wired in.
    assert load_config(str(_ROOT / "config" / "paper.yaml")).scheduler.inter_market_stagger_seconds == 0.5


def test_run_cycle_staggers_market_fanout():
    # 429 FIX: cities sharing a 1 PM anchor are one batch; run_cycle must space their Kalshi reads
    # with one inter-market sleep BEFORE every market except the first (the first fires at t0).
    cfg = load_config(_write_cfg([_CHI, _NYC, _LAX]))
    cfg = dataclasses.replace(
        cfg, scheduler=dataclasses.replace(cfg.scheduler, inter_market_stagger_seconds=0.01))
    sleeps: list[float] = []
    real_sleep = engine.asyncio.sleep
    async def _record(s):                       # capture the stagger calls, don't actually wait
        sleeps.append(s)
        await real_sleep(0)
    engine.asyncio.sleep = _record
    try:
        results = asyncio.run(engine.run_cycle(cfg, None, Book(), _MockKalshi()))
    finally:
        engine.asyncio.sleep = real_sleep
    assert [r.diagnostics["market"] for r in results] == ["CHI", "NYC", "LAX"]   # order preserved
    assert sleeps == [0.01, 0.01], sleeps       # 3 markets -> 2 staggers, none before the first


def test_run_cycle_no_stagger_when_disabled():
    # With the knob at 0 (default), the loop adds no sleeps — pure back-compat for the legacy path.
    cfg = load_config(_write_cfg([_CHI, _NYC]))
    assert cfg.scheduler.inter_market_stagger_seconds == 0.0
    sleeps: list[float] = []
    real_sleep = engine.asyncio.sleep
    async def _record(s):
        sleeps.append(s)
        await real_sleep(0)
    engine.asyncio.sleep = _record
    try:
        asyncio.run(engine.run_cycle(cfg, None, Book(), _MockKalshi()))
    finally:
        engine.asyncio.sleep = real_sleep
    assert sleeps == [], sleeps


def test_preflight_staggers_market_fanout():
    # Clean-restart-logs fix: preflight previews EVERY market (all tz at once), so it must space the
    # reads like run_cycle or it 429-bursts the public endpoint on every restart. Paper -> verify_bankroll
    # is a no-op and the is_live get_positions block is skipped, so _MockKalshi (fetch_event only) suffices.
    cfg = load_config(_write_cfg([_CHI, _NYC, _LAX]))
    cfg = dataclasses.replace(
        cfg, scheduler=dataclasses.replace(cfg.scheduler, inter_market_stagger_seconds=0.01))
    sleeps: list[float] = []
    real_sleep = engine.asyncio.sleep
    async def _record(s):
        sleeps.append(s)
        await real_sleep(0)
    engine.asyncio.sleep = _record
    try:
        asyncio.run(engine.preflight(cfg, Book(), _MockKalshi()))
    finally:
        engine.asyncio.sleep = real_sleep
    assert sleeps == [0.01, 0.01], sleeps       # 3 markets -> one stagger before each after the first


def test_run_cycle_honors_halt():
    cfg = load_config(_write_cfg([_CHI]))
    book = Book(); book.halted_stations = ["KMDW"]
    results = asyncio.run(engine.run_cycle(cfg, None, book, _MockKalshi()))
    assert results[0].strategy.diagnostics.get("halted") and results[0].execution.fills == 0


# --- reporter smoke --------------------------------------------------------

def test_spec_from_market_houston_and_chicago_formats():
    # Houston: subtitle null; range only in yes_sub_title; between/less/greater via strikes.
    assert _spec_from_market({"ticker": "B89.5", "subtitle": None, "yes_sub_title": "89° to 90°",
                              "strike_type": "between", "floor_strike": 89, "cap_strike": 90}) == "89-90"
    assert _spec_from_market({"ticker": "T89", "subtitle": None, "yes_sub_title": "88° or below",
                              "strike_type": "less", "floor_strike": None, "cap_strike": 89}) == "<=88"
    assert _spec_from_market({"ticker": "T96", "subtitle": None, "yes_sub_title": "97° or above",
                              "strike_type": "greater", "floor_strike": 96, "cap_strike": None}) == ">=97"
    # Chicago: subtitle present (unchanged path).
    assert _spec_from_market({"ticker": "B72.5", "subtitle": "72° to 73°", "yes_sub_title": "72° to 73°",
                              "strike_type": "between", "floor_strike": 72, "cap_strike": 73}) == "72-73"
    # No range text at all -> numeric-strike fallback still resolves.
    assert _spec_from_market({"ticker": "B", "subtitle": None, "yes_sub_title": None,
                              "strike_type": "between", "floor_strike": 74, "cap_strike": 75}) == "74-75"
    # Genuinely unparseable -> ValueError (so fetch_event skips it instead of crashing the cycle).
    try:
        _spec_from_market({"ticker": "?", "subtitle": None, "yes_sub_title": None, "strike_type": None})
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_exposure_cap_binds_on_cumulative_stake():
    # C2: execute() must halt a leg once cumulative OPEN exposure + this leg exceeds
    # total_exposure_max_pct * running balance — the process-wide cap the refactor promised.
    cfg = load_config(_write_cfg([_CHI]))                    # paper; total_exposure_max_pct = 0.60
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c = KalshiContract(ticker="KXHIGHCHI-26JUN01-B72.5", event_ticker="KXHIGHCHI-26JUN01",
                       bucket_spec="72-73", subtitle="72° to 73°", strike_type="between", strike=72,
                       yes_bid=0.49, yes_ask=0.50, no_bid=0.49, no_ask=0.50, last_price=0.50,
                       volume_24h=None, open_interest=None, open_time_utc=None, close_time_utc=None,
                       status="active", is_live=True)
    tgt = TargetPosition(ticker=c.ticker, side="yes", target_contracts=4, limit_price_cents=50,
                         bucket_spec="72-73")
    strat = StrategyOutput(targets=[tgt], diagnostics={})

    # cap = 0.60 * $25 = $15 (1500c). Pre-load $14 OPEN exposure on a PRIOR date (counts toward
    # exposure_cents but not today's daily-outlay), so the EXPOSURE gate is the one that binds.
    over = Book()
    over.add_fill("PRELOAD-26MAY30-B70", "yes", 28, 50, 0, "70-71", "x", "2026-05-30", station="KMDW")
    assert over.exposure_cents() == 1400
    r_over = asyncio.run(execute(cfg, pred, [c], strat, over, None, bankroll_usd=25.0))
    assert r_over.fills == 0 and r_over.diagnostics.get("halt_reason") == "exposure_cap", r_over.diagnostics

    # Same leg on a fresh book ($2 << $15 cap) fills normally — the cap doesn't over-block.
    r_ok = asyncio.run(execute(cfg, pred, [c], strat, Book(), None, bankroll_usd=25.0))
    assert r_ok.fills == 1, r_ok.diagnostics


def test_add_fill_does_not_mutate_settled_position():
    # W1: a fill on an already-SETTLED ticker:side must open a fresh position, not blend into
    # the settled row (whose PnL is already banked in realized_pnl_cents).
    b = Book()
    b.add_fill("T-A", "yes", 2, 50, 1, "70-71", "x", "2026-05-30", station="KMDW")
    b.settle("T-A", "yes", won=True, settled_utc="x")
    banked = b.realized_pnl_cents
    b.add_fill("T-A", "yes", 3, 40, 1, "70-71", "y", "2026-05-31", station="KMDW")
    p = b.open_for("T-A", "yes")
    assert not p.settled and p.contracts == 3 and p.avg_cost_cents == 40   # fresh, not blended
    assert b.realized_pnl_cents == banked                                  # settled PnL untouched


def test_daily_outlay_excludes_settled():
    # W7: worst-case-loss-at-risk is OPEN stake only; a settled same-date row must not inflate it.
    b = Book()
    b.add_fill("X-A", "yes", 2, 50, 0, "70-71", "x", "2026-06-01", station="KMDW")
    assert b.daily_outlay_cents("2026-06-01") == 100
    b.settle("X-A", "yes", won=False, settled_utc="x")
    assert b.daily_outlay_cents("2026-06-01") == 0


def test_client_raises_before_opening_on_bad_creds():
    # W6: missing creds must raise BEFORE the httpx client is opened (so it can't leak).
    cfg = KalshiCfg(api_base="https://api.elections.kalshi.com/trade-api/v2",
                    key_id_env="WA_TEST_MISSING_ID", private_key_path_env="WA_TEST_MISSING_KEY",
                    rest_poll_seconds=30, request_timeout_seconds=15)
    os.environ.pop("WA_TEST_MISSING_ID", None)
    os.environ.pop("WA_TEST_MISSING_KEY", None)
    try:
        KalshiClient(cfg, authenticated=True)
        raise AssertionError("expected RuntimeError for missing creds")
    except RuntimeError:
        pass


def test_drawdown_halt_latches_before_first_trade():
    # W4: a drawdown already present in the loaded Book latches BEFORE the loop, so even the
    # first city is gated THIS cycle (not next). -$14 realized on a ~$11 running balance.
    cfg = load_config(_write_cfg([_CHI, _NYC]))
    book = Book()
    book.positions["KXHIGHCHI-26MAY30-B70:yes"] = Position(
        ticker="KXHIGHCHI-26MAY30-B70", side="yes", contracts=1, avg_cost_cents=50,
        total_fees_cents=0, bucket_spec="70-71", opened_utc="x", anchor_date="2026-05-30",
        station="KMDW", settled=True, realized_pnl_cents=-1400)
    book.realized_pnl_cents = -1400
    assert not book.account_halted
    results = asyncio.run(engine.run_cycle(cfg, None, book, _MockKalshi()))
    assert book.account_halted
    assert all(r.strategy.diagnostics.get("halted") for r in results)     # both cities gated, not just SKIP


def test_event_date_code_is_locale_independent():
    # I1: ticker date code must not depend on LC_TIME (strftime %b). Matches en_US output.
    assert event_date_code(pd.Timestamp("2026-06-01")) == "26JUN01"
    assert event_date_code(pd.Timestamp("2026-05-30")) == "26MAY30"
    assert event_date_code(pd.Timestamp("2026-01-09")) == "26JAN09"


def test_scheduler_skips_off_anchor_late_restart():
    # #8: a check well past the anchor window is NOT due (no off-anchor trade). CHI 1 PM CDT = 18z.
    cfg = load_config(_write_cfg([_CHI]))
    s = MarketAnchorScheduler(cfg)
    assert [m.name for m in s.due_markets(cfg.markets, pd.Timestamp("2026-06-15 18:30", tz="UTC"))] == ["CHI"]  # +30m in
    assert s.due_markets(cfg.markets, pd.Timestamp("2026-06-15 19:30", tz="UTC")) == []                          # +90m out


def test_paper_bankroll_excludes_double_fee():
    # #5: realized is already net of fees; paper bankroll must not subtract fees a second time.
    cfg = load_config(_write_cfg([_CHI]))
    book = Book()
    book.add_fill("X", "yes", 2, 50, 3, "70-71", "x", "2026-05-30", station="KMDW")
    book.settle("X", "yes", won=True, settled_utc="x")          # net realized = (100-50)*2 - 3 = 97c
    assert abs(engine._current_bankroll(cfg, book) - 25.97) < 1e-6, engine._current_bankroll(cfg, book)


def test_run_cycle_drops_a_failed_market_from_results():
    # #2: a market whose fetch throws is isolated and DROPPED from results, so the resident loop
    # won't record it as fired (it retries next tick) — verified by its absence from the results.
    cfg = load_config(_write_cfg([_CHI, _NYC]))
    results = asyncio.run(engine.run_cycle(cfg, None, Book(), _ThrowingKalshi("KXHIGHNY")))
    got = [r.diagnostics["market"] for r in results]
    assert "CHI" in got and "NYC" not in got, got


def test_event_guard_rejects_mismatched_event_ticker():
    # #7: with event_ticker sourced from the API, a response for the wrong event is now caught.
    anchor = pd.Timestamp("2026-06-01")
    wrong = KalshiContract(ticker="KXHIGHCHI-26MAY30-B72.5", event_ticker="KXHIGHCHI-26MAY30",
                           bucket_spec="72-73", subtitle="72° to 73°", strike_type="between", strike=72,
                           yes_bid=0.49, yes_ask=0.50, no_bid=0.49, no_ask=0.50, last_price=0.50,
                           volume_24h=None, open_interest=None, open_time_utc=None,
                           close_time_utc=pd.Timestamp.now(tz="UTC") + pd.Timedelta(hours=6),
                           status="active", is_live=True)
    _, skip = engine._tradeable_contracts([wrong], anchor)
    assert skip is not None and "does not match" in skip, skip


def test_live_exchange_truth_delta_and_cancel_on_no_fill():
    cfg = load_config(_write_cfg([_CHI], mode="live"), require_live_creds=False)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c = _live_contract("KXHIGHCHI-26JUN01-B72.5")
    tgt = TargetPosition(ticker=c.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="72-73")
    strat = StrategyOutput(targets=[tgt], diagnostics={})
    # #3: exchange already holds the full target; the (stale) Book thinks 0 -> delta 0 -> NO order.
    cli = _MockLiveClient(held={(c.ticker, "yes"): (3, 50)})
    r = asyncio.run(execute(cfg, pred, [c], strat, Book(), cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert cli.placed == [] and r.fills == 0, (cli.placed, r.diagnostics)
    # #4: order placed, nothing fills -> the resting order is CANCELLED.
    cli2 = _MockLiveClient()
    r2 = asyncio.run(execute(cfg, pred, [c], strat, Book(), cli2, market=cfg.markets[0], bankroll_usd=25.0))
    assert len(cli2.placed) == 1 and len(cli2.cancelled) == 1 and r2.fills == 0, (cli2.placed, cli2.cancelled)


def test_live_wing_atomic_vs_budget_places_zero_when_it_cannot_fully_fit():
    # W2: a wing is ATOMIC w.r.t. the order budget. cap=1 but the wing has 2 legs -> the whole
    # wing won't fit, so ZERO legs are placed (not 1) — a cap trip can't leave a naked single leg.
    # (Replaces the old per-leg #10 test, whose "place leg 1, halt leg 2" expectation was exactly
    # the half-placed-wing failure W2 fixes; cross-wing budget consumption is covered by
    # test_per_cycle_order_budget_sums_across_markets.)
    cfg = load_config(_write_cfg([_CHI], mode="live", per_anchor=1), require_live_creds=False)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c1, c2 = _live_contract("KXHIGHCHI-26JUN01-B72.5"), _live_contract("KXHIGHCHI-26JUN01-B74.5")
    strat = StrategyOutput(targets=[
        TargetPosition(ticker=c1.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="72-73"),
        TargetPosition(ticker=c2.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="74-75"),
    ], diagnostics={})
    cli = _MockLiveClient()    # nothing fills
    r = asyncio.run(execute(cfg, pred, [c1, c2], strat, Book(), cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert cli.placed == [] and r.diagnostics.get("halt_reason") == "max_trades", (cli.placed, r.diagnostics)
    assert r.fills == 0 and r.skipped == 2     # both legs skipped, none placed


def test_live_books_marginal_price_on_topup():
    # #6: topping up an existing holding books the MARGINAL fill price, not the blended avg.
    cfg = load_config(_write_cfg([_CHI], mode="live"), require_live_creds=False)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c = _live_contract("KXHIGHCHI-26JUN01-B72.5")
    cli = _MockLiveClient(held={(c.ticker, "yes"): (2, 40)}, fills={c.ticker: 2})   # hold 2@40; top-up fills 2@50
    strat = StrategyOutput(targets=[
        TargetPosition(ticker=c.ticker, side="yes", target_contracts=4, limit_price_cents=50, bucket_spec="72-73")
    ], diagnostics={})
    book = Book()
    asyncio.run(execute(cfg, pred, [c], strat, book, cli, market=cfg.markets[0], bankroll_usd=25.0))
    p = book.open_for(c.ticker, "yes")
    assert p is not None and p.contracts == 2 and p.avg_cost_cents == 50, (p and (p.contracts, p.avg_cost_cents))


def test_per_cycle_order_budget_sums_across_markets():
    # W1: per_anchor_max_trades is a per-CYCLE backstop SHARED across cities (was per-market). With
    # cap=6 and two 4-leg wings: a per-MARKET cap would place 8 (4+4); the shared cap caps the cycle.
    # Because a wing is ATOMIC (W2), CHI's 4-leg wing fits (budget 6 -> places 4, 2 left) but NYC's
    # 4-leg wing needs 4 > the 2 remaining, so NYC places 0 -> total 4 (NOT 8; and not 6, since a
    # wing can't be split to consume the last 2 of the budget). 4 != 8 proves the budget is shared.
    cfg = load_config(_write_cfg([_CHI, _NYC], mode="live", per_anchor=6), require_live_creds=False)

    def _legs(ev):
        # execute() doesn't inspect event_ticker (that's the engine's _tradeable_contracts), so a
        # parameterized ticker is all these direct-execute tests need. limit must be >= the
        # contract's 50c ask or _execute_one rejects ("won't pay through our limit").
        cs = [_live_contract(f"{ev}-26JUN01-B{b}") for b in (70, 72, 74, 76)]
        tg = [TargetPosition(ticker=c.ticker, side="yes", target_contracts=1,
                             limit_price_cents=50, bucket_spec="70-71") for c in cs]
        return cs, tg

    chi_cs, chi_tg = _legs("KXHIGHCHI")
    nyc_cs, nyc_tg = _legs("KXHIGHNY")
    budget = RunBudget(cap=cfg.risk.per_anchor_max_trades)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    cli = _MockLiveClient(fills={c.ticker: 1 for c in chi_cs + nyc_cs})   # every leg fills

    r1 = asyncio.run(execute(cfg, pred, chi_cs, StrategyOutput(targets=chi_tg, diagnostics={}),
                             Book(), cli, market=cfg.markets[0], bankroll_usd=25.0, budget=budget))
    r2 = asyncio.run(execute(cfg, pred, nyc_cs, StrategyOutput(targets=nyc_tg, diagnostics={}),
                             Book(), cli, market=cfg.markets[1], bankroll_usd=25.0, budget=budget))
    assert r1.fills == 4 and r2.fills == 0, (r1.fills, r2.fills)
    assert len(cli.placed) == 4, cli.placed                # 4 (CHI) + 0 (NYC) — shared cap, atomic wing
    assert r2.diagnostics.get("halt_reason") == "max_trades"


def test_per_cycle_order_budget_two_wings_both_fit():
    # W1 companion: cap=8 with two 4-leg wings -> BOTH wings fit (4+4=8) -> 8 placed. Confirms the
    # shared budget isn't over-restrictive: it binds only when the cumulative cycle demand exceeds it.
    cfg = load_config(_write_cfg([_CHI, _NYC], mode="live", per_anchor=8), require_live_creds=False)
    chi = [_live_contract(f"KXHIGHCHI-26JUN01-B{b}") for b in (70, 72, 74, 76)]
    nyc = [_live_contract(f"KXHIGHNY-26JUN01-B{b}") for b in (70, 72, 74, 76)]
    mk = lambda cs: [TargetPosition(ticker=c.ticker, side="yes", target_contracts=1,
                                    limit_price_cents=50, bucket_spec="70-71") for c in cs]
    budget = RunBudget(cap=8)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    cli = _MockLiveClient(fills={c.ticker: 1 for c in chi + nyc})
    r1 = asyncio.run(execute(cfg, pred, chi, StrategyOutput(targets=mk(chi), diagnostics={}),
                             Book(), cli, market=cfg.markets[0], bankroll_usd=25.0, budget=budget))
    r2 = asyncio.run(execute(cfg, pred, nyc, StrategyOutput(targets=mk(nyc), diagnostics={}),
                             Book(), cli, market=cfg.markets[1], bankroll_usd=25.0, budget=budget))
    assert r1.fills == 4 and r2.fills == 4 and len(cli.placed) == 8, (r1.fills, r2.fills, cli.placed)


def test_wing_atomic_when_full_wing_exceeds_outlay_cap_places_zero():
    # W2(a): if the FULL wing's incremental cost exceeds a cap, ZERO legs are placed (not a
    # partial wing). 3-leg wing @ 50c x3 = $4.50 > a $4.00 daily-outlay cap -> skip the whole wing.
    cfg = load_config(_write_cfg([_CHI], mode="live", per_anchor=6), require_live_creds=False)
    import yaml as _yaml
    raw = _yaml.safe_load(Path(cfg.config_path).read_text(encoding="utf-8"))
    raw["risk"]["daily_max_loss_usd"] = 4.0
    p = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    _yaml.safe_dump(raw, p); p.close()
    cfg = load_config(p.name, require_live_creds=False)

    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    cs = [_live_contract(f"KXHIGHCHI-26JUN01-B{b}") for b in (70, 72, 74)]
    tg = [TargetPosition(ticker=c.ticker, side="yes", target_contracts=3,
                         limit_price_cents=50, bucket_spec="70-71") for c in cs]
    cli = _MockLiveClient(fills={c.ticker: 3 for c in cs})
    r = asyncio.run(execute(cfg, pred, cs, StrategyOutput(targets=tg, diagnostics={}),
                            Book(), cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert cli.placed == [] and r.fills == 0 and r.skipped == 3, (cli.placed, r.diagnostics)
    assert r.diagnostics.get("halt_reason") == "daily_outlay_cap"


def test_wing_atomic_kill_armed_before_wing_places_zero(tmp_path):
    # W2(b): the kill switch is checked at WING granularity (before the wing). Armed before the
    # wing -> zero legs placed, none half-filled. (test_live_exchange... + the gate handle the
    # pre-cycle hard-abort path; this asserts the in-execute gate skips the whole wing cleanly.)
    cfg = load_config(_write_cfg([_CHI], mode="live", per_anchor=6), require_live_creds=False)
    Path(cfg.paths.kill_switch).parent.mkdir(parents=True, exist_ok=True)
    Path(cfg.paths.kill_switch).write_text("halt", encoding="utf-8")
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c = _live_contract("KXHIGHCHI-26JUN01-B72.5")
    tg = [TargetPosition(ticker=c.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="72-73")]
    cli = _MockLiveClient(fills={c.ticker: 3})
    # _check_kill_switch raises pre-cycle; execute() surfaces KillSwitchTripped to the caller.
    raised = False
    try:
        asyncio.run(execute(cfg, pred, [c], StrategyOutput(targets=tg, diagnostics={}),
                            Book(), cli, market=cfg.markets[0], bankroll_usd=25.0))
    except KillSwitchTripped:
        raised = True
    assert raised and cli.placed == []          # hard-abort before any leg


def test_wing_atomic_kill_armed_midcycle_skips_whole_wing(tmp_path):
    # W2(b) cont.: arm the kill switch AFTER the pre-cycle check passes but before placement.
    # The wing-level gate (_gate_wing) re-checks the file and skips the ENTIRE wing (0 legs),
    # never a single naked leg. Simulated by arming inside the mock's first get_positions call.
    cfg = load_config(_write_cfg([_CHI], mode="live", per_anchor=6), require_live_creds=False)
    Path(cfg.paths.kill_switch).parent.mkdir(parents=True, exist_ok=True)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c = _live_contract("KXHIGHCHI-26JUN01-B72.5")
    tg = [TargetPosition(ticker=c.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="72-73")]

    class _ArmOnSnapshot(_MockLiveClient):
        async def get_positions(self):
            Path(cfg.paths.kill_switch).write_text("halt", encoding="utf-8")  # arm between pre-check and gate
            return await super().get_positions()

    cli = _ArmOnSnapshot(fills={c.ticker: 3})
    r = asyncio.run(execute(cfg, pred, [c], StrategyOutput(targets=tg, diagnostics={}),
                            Book(), cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert cli.placed == [] and r.fills == 0, (cli.placed, r.diagnostics)
    assert r.diagnostics.get("halt_reason") == "kill_switch"


def test_partial_fill_cancels_remainder_and_unique_coid_allows_completion():
    # W3: order 3, only 2 fill -> Book records 2, the resting remainder is cancelled, AND a later
    # cycle (fresh unique coid) CAN place the remaining 1 (not duplicate-rejected).
    cfg = load_config(_write_cfg([_CHI], mode="live"), require_live_creds=False)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c = _live_contract("KXHIGHCHI-26JUN01-B72.5")
    tg = [TargetPosition(ticker=c.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="72-73")]
    book = Book()

    cli = _MockLiveClient(fills={c.ticker: 2})    # 2 of 3 fill on the first cycle
    r1 = asyncio.run(execute(cfg, pred, [c], StrategyOutput(targets=tg, diagnostics={}),
                             book, cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert r1.fills == 1 and book.open_for(c.ticker, "yes").contracts == 2     # booked exactly 2
    assert len(cli.cancelled) == 1                                              # remainder cancelled
    coid_1 = cli.coids[0]

    # Next cycle: exchange now holds 2 (from cli._held), the missing 1 fills under a NEW coid.
    cli._fills = {c.ticker: 1}
    cli.cancelled.clear()
    r2 = asyncio.run(execute(cfg, pred, [c], StrategyOutput(targets=tg, diagnostics={}),
                             book, cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert r2.fills == 1 and book.open_for(c.ticker, "yes").contracts == 3     # completed to 3
    coid_2 = cli.coids[-1]
    assert coid_1 != coid_2 and coid_2.startswith(f"wa-2026-06-01-{c.ticker}-yes-")   # unique per cycle


def test_mid_wing_leg_error_flattens_filled_legs_and_squares_book():
    # W2: leg 1 fills, leg 2's place_order raises -> the filled leg 1 is FLATTENED (offsetting
    # sell) and removed from the Book so it matches the (now-flat) exchange. The day never ends
    # on a naked directional leg.
    cfg = load_config(_write_cfg([_CHI], mode="live"), require_live_creds=False)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c1, c2 = _live_contract("KXHIGHCHI-26JUN01-B72.5"), _live_contract("KXHIGHCHI-26JUN01-B74.5")
    tg = [TargetPosition(ticker=c1.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="72-73"),
          TargetPosition(ticker=c2.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="74-75")]

    class _ErrOnSecond(_MockLiveClient):
        async def place_order(self, *, ticker, side, action, count, limit_price_cents, client_order_id=None):
            if action == "buy" and ticker == c2.ticker:
                raise RuntimeError("simulated exchange rejection on leg 2")
            return await super().place_order(ticker=ticker, side=side, action=action, count=count,
                                             limit_price_cents=limit_price_cents, client_order_id=client_order_id)

    cli = _ErrOnSecond(fills={c1.ticker: 3})    # leg 1 fills 3; leg 2 raises
    book = Book()
    r = asyncio.run(execute(cfg, pred, [c1, c2], StrategyOutput(targets=tg, diagnostics={}),
                            book, cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert r.diagnostics.get("halt_reason") == "leg_error", r.diagnostics
    assert book.open_for(c1.ticker, "yes") is None             # leg 1 removed (flattened) from the Book
    assert book.exposure_cents() == 0 and book.fees_paid_cents == 0   # Book squared, entry fee reversed
    assert cli.flatten_sells == [(c1.ticker, "yes", 3)]        # leg 1 flattened with a same-size sell


def test_mid_wing_flatten_failure_keeps_leg_in_book():
    # W2: if the flatten SELL itself fails, the naked leg is KEPT in the Book (it is still held on
    # the exchange) and a CRITICAL is logged — Book and exchange stay consistent (both hold it).
    cfg = load_config(_write_cfg([_CHI], mode="live"), require_live_creds=False)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c1, c2 = _live_contract("KXHIGHCHI-26JUN01-B72.5"), _live_contract("KXHIGHCHI-26JUN01-B74.5")
    tg = [TargetPosition(ticker=c1.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="72-73"),
          TargetPosition(ticker=c2.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="74-75")]

    class _ErrOnSecondAndSell(_MockLiveClient):
        async def place_order(self, *, ticker, side, action, count, limit_price_cents, client_order_id=None):
            if action == "buy" and ticker == c2.ticker:
                raise RuntimeError("simulated rejection on leg 2 buy")
            if action == "sell":
                raise RuntimeError("simulated rejection on flatten sell")
            return await super().place_order(ticker=ticker, side=side, action=action, count=count,
                                             limit_price_cents=limit_price_cents, client_order_id=client_order_id)

    cli = _ErrOnSecondAndSell(fills={c1.ticker: 3})
    book = Book()
    r = asyncio.run(execute(cfg, pred, [c1, c2], StrategyOutput(targets=tg, diagnostics={}),
                            book, cli, market=cfg.markets[0], bankroll_usd=25.0))
    held = book.open_for(c1.ticker, "yes")
    assert held is not None and held.contracts == 3            # KEPT — still held on the exchange
    assert r.diagnostics.get("halt_reason") == "leg_error"


def test_flatten_sells_full_holding_not_just_cycle_delta():
    # W-A: on a TOP-UP wing (a prior-cycle holding already exists, this cycle adds more) a mid-wing
    # error must flatten the FULL exchange holding, not just this cycle's delta fill — else
    # remove_position (which drops the whole position) leaves the remainder naked on the exchange
    # while the Book reads flat.
    cfg = load_config(_write_cfg([_CHI], mode="live"), require_live_creds=False)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c1, c2 = _live_contract("KXHIGHCHI-26JUN01-B72.5"), _live_contract("KXHIGHCHI-26JUN01-B74.5")
    # Prior cycle already bought 3 of leg 1 (in the Book + on the exchange); this cycle tops up to 5.
    book = Book()
    book.add_fill(ticker=c1.ticker, side="yes", contracts=3, fill_cents=50, fee_cents=0,
                  bucket_spec="72-73", opened_utc="2026-06-01T00:00:00+00:00",
                  anchor_date="2026-06-01", station="KMDW")
    tg = [TargetPosition(ticker=c1.ticker, side="yes", target_contracts=5, limit_price_cents=50, bucket_spec="72-73"),
          TargetPosition(ticker=c2.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="74-75")]

    class _ErrOnSecond(_MockLiveClient):
        async def place_order(self, *, ticker, side, action, count, limit_price_cents, client_order_id=None):
            if action == "buy" and ticker == c2.ticker:
                raise RuntimeError("simulated rejection on leg 2")
            return await super().place_order(ticker=ticker, side=side, action=action, count=count,
                                             limit_price_cents=limit_price_cents, client_order_id=client_order_id)

    # exchange already holds 3 of leg 1; the top-up delta (5-3=2) fills -> exchange + Book reach 5.
    cli = _ErrOnSecond(held={(c1.ticker, "yes"): (3, 50)}, fills={c1.ticker: 2})
    r = asyncio.run(execute(cfg, pred, [c1, c2], StrategyOutput(targets=tg, diagnostics={}),
                            book, cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert r.diagnostics.get("halt_reason") == "leg_error"
    assert cli.flatten_sells == [(c1.ticker, "yes", 5)], cli.flatten_sells   # FULL 5, not the delta 2
    assert book.open_for(c1.ticker, "yes") is None                          # whole position removed


def test_live_books_exchange_reported_fee_when_present():
    # W4: when the order ack carries the exchange fee AND its fill_count matches the polled fill,
    # the Book records the EXCHANGE fee, not the formula estimate.
    cfg = load_config(_write_cfg([_CHI], mode="live"), require_live_creds=False)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c = _live_contract("KXHIGHCHI-26JUN01-B72.5")
    tg = [TargetPosition(ticker=c.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="72-73")]
    # Formula fee for 3 @ $0.50 = ceil(0.07*3*0.5*0.5*100) = ceil(5.25) = 6c. Inject a different
    # exchange fee ($0.04 = 4c) so we can tell which one the Book stored.
    cli = _MockLiveClient(fills={c.ticker: 3}, ack_fee={c.ticker: 0.04})
    book = Book()
    asyncio.run(execute(cfg, pred, [c], StrategyOutput(targets=tg, diagnostics={}),
                        book, cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert book.fees_paid_cents == 4, book.fees_paid_cents      # exchange 4c, not the 6c estimate


def test_w4_recon_overwrites_estimated_fee_with_exchange_truth():
    # W4 RECON: Book.reconcile_fees overwrites an open position's ESTIMATED fee with the exchange-truth
    # fees_paid_cents (/portfolio/positions), so settlement later subtracts the exact fee. A non-positive
    # exchange fee is treated as "no data" and skipped (keeps the estimate).
    from weather_alpha.kalshi import KalshiPosition
    book = Book()
    book.add_fill(ticker="KXHIGHCHI-26JUN01-B72.5", side="yes", contracts=3, fill_cents=50,
                  fee_cents=6, bucket_spec="72-73", opened_utc="2026-06-01T00:00:00+00:00",
                  anchor_date="2026-06-01", station="KMDW")
    assert book.fees_paid_cents == 6                              # bot's ceil(7%) estimate
    exch = [KalshiPosition(ticker="KXHIGHCHI-26JUN01-B72.5", side="yes", contracts=3,
                           avg_price_cents=50, realized_pnl_cents=0, fees_paid_cents=4)]
    net = book.reconcile_fees(exch)                               # exchange truth = 4c
    assert net == -2 and book.fees_paid_cents == 4               # account total now exact
    assert book.open_for("KXHIGHCHI-26JUN01-B72.5", "yes").total_fees_cents == 4
    # a 0 (absent/legacy) exchange fee is ignored -> estimate kept (can't zero a real fee)
    book2 = Book()
    book2.add_fill(ticker="X-Y", side="yes", contracts=2, fill_cents=40, fee_cents=5,
                   bucket_spec="1-2", opened_utc="t", anchor_date="2026-06-01", station="S")
    book2.reconcile_fees([KalshiPosition(ticker="X-Y", side="yes", contracts=2, avg_price_cents=40)])
    assert book2.fees_paid_cents == 5                             # unchanged (0 -> skipped)


def _http_400(code):
    # build an httpx.HTTPStatusError carrying a Kalshi-style {"error":{"code":...}} 400 body
    resp = httpx.Response(400, json={"error": {"code": code, "message": code}},
                          request=httpx.Request("POST", "http://x"))
    return httpx.HTTPStatusError(code, request=resp.request, response=resp)


def test_place_order_retries_on_400_rate_throttle():
    # ORDER-RATE HARDENING: a 400 invalid_parameters (Kalshi's order-burst throttle, observed in live
    # testing) is retried with backoff and succeeds once the throttle clears — instead of bubbling up
    # as a hard leg error that would trigger a flatten/skip. Re-sending the same client_order_id is
    # safe because a 400 means the order was NOT created.
    cli = KalshiClient(load_config(_write_cfg([_CHI], mode="live"), require_live_creds=False).kalshi,
                       authenticated=False)
    n = {"calls": 0}
    async def fake(method, path, *, json=None):
        n["calls"] += 1
        if n["calls"] == 1:
            raise _http_400("invalid_parameters")
        return {"order": {"order_id": "ok-after-retry"}}
    cli._signed_request = fake
    async def run():
        try:
            return await cli.place_order(ticker="KXHIGHCHI-26JUN01-B72.5", side="yes",
                                         action="buy", count=1, limit_price_cents=50)
        finally:
            await cli.close()
    ack = asyncio.run(run())
    assert n["calls"] == 2 and ack["order"]["order_id"] == "ok-after-retry"


def test_place_order_does_not_retry_non_rate_400():
    # a NON-rate 400 (e.g. invalid_order — a genuinely malformed order) is raised immediately, NOT
    # retried, so a real order bug surfaces fast instead of being masked by the throttle retries.
    cli = KalshiClient(load_config(_write_cfg([_CHI], mode="live"), require_live_creds=False).kalshi,
                       authenticated=False)
    n = {"calls": 0}
    async def fake(method, path, *, json=None):
        n["calls"] += 1
        raise _http_400("invalid_order")
    cli._signed_request = fake
    async def run():
        try:
            await cli.place_order(ticker="X", side="yes", action="buy", count=1, limit_price_cents=50)
        finally:
            await cli.close()
    try:
        asyncio.run(run())
        assert False, "expected HTTPStatusError to propagate"
    except httpx.HTTPStatusError:
        pass
    assert n["calls"] == 1                                        # raised on first attempt, no retry


class _FakeHttp:
    """Stand-in for KalshiClient._http: returns queued responses for .get(); used by the read-retry
    tests. Each .get() pops the next status from `seq` (last one repeats)."""
    def __init__(self, seq):
        self._seq = list(seq)
        self.calls = 0
    async def get(self, path, params=None):
        self.calls += 1
        status = self._seq[min(self.calls - 1, len(self._seq) - 1)]
        body = {"markets": []} if status == 200 else {}
        return httpx.Response(status, json=body, headers={"Retry-After": "0"},
                              request=httpx.Request("GET", "http://x" + path))
    async def aclose(self):
        pass


def test_fetch_event_retries_429_then_succeeds():
    # READ-PATH 429 HARDENING: the unauthenticated market-data GET shares Kalshi's low per-IP tier with
    # the orderbook logger; a 429 is retried (honoring Retry-After) and succeeds, so a same-tz fan-out /
    # the preflight preview doesn't litter the log with tracebacks. GETs are idempotent -> retry is safe.
    cli = KalshiClient(load_config(_write_cfg([_CHI])).kalshi, authenticated=False)
    cli._http = _FakeHttp([429, 429, 200])
    async def run():
        try:
            return await cli.fetch_event(pd.Timestamp("2026-06-01"), "KXHIGHCHI")
        finally:
            await cli.close()
    out = asyncio.run(run())
    assert cli._http.calls == 3 and out == []        # two 429s retried, third 200 -> empty event


def test_fetch_event_raises_after_persistent_429():
    # A persistent 429 (tier saturated) must still surface after the retry budget — not hang, not be
    # swallowed — so the engine isolates that city and retries it next cycle.
    cli = KalshiClient(load_config(_write_cfg([_CHI])).kalshi, authenticated=False)
    cli._http = _FakeHttp([429])
    async def run():
        try:
            await cli.fetch_event(pd.Timestamp("2026-06-01"), "KXHIGHCHI")
        finally:
            await cli.close()
    try:
        asyncio.run(run())
        assert False, "expected HTTPStatusError after exhausting retries"
    except httpx.HTTPStatusError:
        pass
    assert cli._http.calls == 4                       # _READ_RETRIES attempts, then raise


def test_cycle_level_daily_loss_gate_blocks_orders_but_runs_settlement():
    # I2: a breached account-wide daily-loss accumulator gates the WHOLE cycle once (no new orders
    # for any city) while settlement still runs. Unit-check the gate fn + the run_cycle integration.
    from weather_alpha.execution import check_daily_loss, KillSwitchTripped as _KST
    cfg = load_config(_write_cfg([_CHI, _NYC]))
    over_cap = -int(cfg.risk.daily_max_loss_usd * 100) - 1
    recent = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
    old = (pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)).strftime("%Y-%m-%d")
    clean = Book()
    check_daily_loss(cfg, clean)                       # no breach -> no raise
    breached = Book()
    breached.daily_loss_cents = {recent: over_cap}     # RECENT breach -> gates
    raised = False
    try:
        check_daily_loss(cfg, breached)
    except _KST:
        raised = True
    assert raised
    # I-A: a breach on an OLD date must NOT gate (else a single bad day latches the bot off forever
    # with no reset path). The 7-day rolling window ages it out.
    aged = Book()
    aged.daily_loss_cents = {old: over_cap}
    check_daily_loss(cfg, aged)                         # no raise -> aged out
    # Integration: run_cycle on a recently-breached book gates every city (diagnostics carry the gate).
    results = asyncio.run(engine.run_cycle(cfg, None, breached, _MockKalshi()))
    assert results and all(r.execution.diagnostics.get("gated") for r in results), \
        [r.execution.diagnostics for r in results]
    assert all(r.execution.fills == 0 for r in results)


def test_positions_parse_realized_pnl_and_fees():
    # W4: _parse_positions surfaces realized_pnl_dollars + fees_paid_dollars (verified field names)
    # as cents on KalshiPosition.
    from weather_alpha.kalshi import _parse_positions
    payload = {"market_positions": [{
        "ticker": "KXHIGHCHI-26JUN01-B72.5", "position_fp": "3.00",
        "market_exposure_dollars": "1.5000", "realized_pnl_dollars": "0.8700",
        "fees_paid_dollars": "0.0300",
    }]}
    pos = _parse_positions(payload)
    assert len(pos) == 1 and pos[0].realized_pnl_cents == 87 and pos[0].fees_paid_cents == 3
    assert pos[0].contracts == 3 and pos[0].avg_price_cents == 50


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
