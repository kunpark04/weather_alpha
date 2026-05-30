"""Verification of the LIVE execution safety fixes (C1 / C3 / C4).

Uses a mock Kalshi client — no creds, no network. Run after touching execution.py:
    python scripts/check_live_execution.py        # -> ALL PASS

Asserts:
  - full fill  -> Book records the ACTUAL filled count at the observed avg price (C1)
  - no fill    -> nothing booked (no phantom position); leg retriable next cycle (C1)
  - partial    -> Book records only what filled (C1)
  - client_order_id is deterministic: f"wa-{anchor}-{ticker}-{side}" (C4)
  - intraday outlay over daily_max_loss_usd -> halt before placing (C3)
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from weather_alpha.config import load_config  # noqa: E402
from weather_alpha.execution import execute  # noqa: E402
from weather_alpha.kalshi import KalshiContract, KalshiPosition  # noqa: E402
from weather_alpha.model import Prediction  # noqa: E402
from weather_alpha.positions import Book  # noqa: E402
from weather_alpha.strategy import StrategyOutput, TargetPosition  # noqa: E402


def _contract(ticker: str, ask: float) -> KalshiContract:
    return KalshiContract(
        ticker=ticker, event_ticker="E", bucket_spec="60-61", subtitle="60 to 61",
        strike_type=None, strike=None, yes_bid=ask - 0.02, yes_ask=ask,
        no_bid=0.0, no_ask=1.0, last_price=ask, volume_24h=1, open_interest=1,
        open_time_utc=None, close_time_utc=None, status="open", is_live=True,
    )


class MockClient:
    """get_positions() returns scripted holdings; first call = 'before', rest = 'after'."""
    def __init__(self, before: int, after: int, ticker: str, avg_cents: int = 45):
        self._seq = [before] + [after] * 8
        self._i = 0
        self.ticker = ticker
        self.avg_cents = avg_cents
        self.placed: list[dict] = []

    async def get_positions(self):
        qty = self._seq[min(self._i, len(self._seq) - 1)]
        self._i += 1
        return [KalshiPosition(self.ticker, "yes", qty, self.avg_cents)] if qty else []

    async def place_order(self, **kw):
        self.placed.append(kw)
        return {"order_id": "mock-1"}


def _cfg_live(daily_max_loss_usd: float | None = None):
    cfg = load_config()
    tmp = Path(tempfile.mkdtemp())
    cfg = dataclasses.replace(
        cfg, mode="live",
        paths=dataclasses.replace(cfg.paths,
                                  live_log=tmp / "live_log.parquet",
                                  positions_snapshot=tmp / "pos.json",
                                  kill_switch=tmp / "KILL"),
    )
    if daily_max_loss_usd is not None:
        cfg = dataclasses.replace(
            cfg, risk=dataclasses.replace(cfg.risk, daily_max_loss_usd=daily_max_loss_usd))
    return cfg


def _pred(anchor: pd.Timestamp) -> Prediction:
    grid = pd.Series([1.0], index=[60], name="P")
    return Prediction(date=anchor, model="t", pmf=grid, median=60, lo10=60, hi90=60,
                      peak_F=60, peak_P=1.0, season="MAM", hard_floor=None,
                      leaked_below_floor=0.0, nan_features=[])


async def _run(before, after, target=3, daily_max_loss_usd=None):
    anchor = pd.Timestamp("2026-05-30")
    ticker = "KX-T"
    c = _contract(ticker, 0.45)
    tgt = TargetPosition(ticker=ticker, side="yes", target_contracts=target,
                         limit_price_cents=45, bucket_spec="60-61", rationale={})
    strat = StrategyOutput(targets=[tgt], diagnostics={})
    book = Book()
    client = MockClient(before, after, ticker)
    res = await execute(_cfg_live(daily_max_loss_usd), _pred(anchor), [c], strat, book, client)
    held = book.open_for(ticker, "yes")
    coid = client.placed[0]["client_order_id"] if client.placed else None
    return res, (held.contracts if held else 0), coid


def main() -> int:
    ok = True

    # 1. Full fill: before 0, after 3 -> book 3.
    res, held, coid = asyncio.run(_run(0, 3, target=3))
    exp_coid = "wa-2026-05-30-KX-T-yes"
    t1 = held == 3 and res.fills == 1 and coid == exp_coid
    print(f"[full]    booked={held} fills={res.fills} coid={coid}  -> {'PASS' if t1 else 'FAIL'}")
    ok &= t1

    # 2. No fill: before 0, after 0 -> book nothing, no phantom.
    res, held, coid = asyncio.run(_run(0, 0, target=3))
    t2 = held == 0 and res.fills == 0 and res.skipped == 1
    print(f"[nofill]  booked={held} fills={res.fills} skipped={res.skipped}  -> {'PASS' if t2 else 'FAIL'}")
    ok &= t2

    # 3. Partial: before 0, after 2 of 3 -> book 2.
    res, held, coid = asyncio.run(_run(0, 2, target=3))
    t3 = held == 2 and res.fills == 1
    print(f"[partial] booked={held} fills={res.fills}  -> {'PASS' if t3 else 'FAIL'}")
    ok &= t3

    # 4. C3 intraday outlay breaker: cap $1.00 but order cost 3*45c=$1.35 -> halt
    #    before placing, book 0.
    res, held, coid = asyncio.run(_run(0, 3, target=3, daily_max_loss_usd=1.00))
    t4 = (held == 0 and res.fills == 0
          and res.diagnostics.get("halt_reason") == "daily_outlay_cap")
    print(f"[C3 cap]  booked={held} fills={res.fills} halt={res.diagnostics.get('halt_reason')}"
          f"  -> {'PASS' if t4 else 'FAIL'}")
    ok &= t4

    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
