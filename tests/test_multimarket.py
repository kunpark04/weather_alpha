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
from weather_alpha.config import load_config, KalshiCfg                    # noqa: E402
from weather_alpha.execution import ExecutionResult, execute, reconcile_settlements  # noqa: E402
from weather_alpha.kalshi import _spec_from_market, KalshiContract, KalshiClient, event_date_code, KalshiPosition  # noqa: E402
from weather_alpha.positions import Book, Position                          # noqa: E402
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
    """Minimal authenticated client for LIVE-path execute() tests (#3/#4/#6/#10)."""
    def __init__(self, held=None, fills=None):
        self._held = dict(held or {})        # (ticker, side) -> (contracts, avg_cents) on the "exchange"
        self._fills = dict(fills or {})       # ticker -> qty that fills when an order is placed
        self.placed: list = []
        self.cancelled: list = []

    async def get_positions(self):
        return [KalshiPosition(ticker=t, side=s, contracts=c, avg_price_cents=a)
                for (t, s), (c, a) in self._held.items() if c]

    async def place_order(self, *, ticker, side, action, count, limit_price_cents, client_order_id=None):
        self.placed.append((ticker, side, count))
        q = self._fills.get(ticker, 0)
        if q:
            c0, a0 = self._held.get((ticker, side), (0, 0))
            c1 = c0 + q
            self._held[(ticker, side)] = (c1, int(round((c0 * a0 + q * limit_price_cents) / c1)))
        return {"order_id": f"oid-{ticker}"}

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


def test_live_per_anchor_cap_counts_attempts_not_fills():
    # #10: a no-fill order still consumes the per-anchor cap. cap=1 + two no-fill legs ->
    # leg 1 attempted, leg 2 halted by the cap.
    cfg = load_config(_write_cfg([_CHI], mode="live", per_anchor=1), require_live_creds=False)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp("2026-06-01"))
    c1, c2 = _live_contract("KXHIGHCHI-26JUN01-B72.5"), _live_contract("KXHIGHCHI-26JUN01-B74.5")
    strat = StrategyOutput(targets=[
        TargetPosition(ticker=c1.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="72-73"),
        TargetPosition(ticker=c2.ticker, side="yes", target_contracts=3, limit_price_cents=50, bucket_spec="74-75"),
    ], diagnostics={})
    cli = _MockLiveClient()    # nothing fills
    r = asyncio.run(execute(cfg, pred, [c1, c2], strat, Book(), cli, market=cfg.markets[0], bankroll_usd=25.0))
    assert len(cli.placed) == 1 and r.diagnostics.get("halt_reason") == "max_trades", (cli.placed, r.diagnostics)


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
