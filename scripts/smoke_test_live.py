"""LIVE smoke test (option 2) — validate the engine-hardening order path against the REAL Kalshi
exchange, on cheap NON-CHI buckets, HARD-CAPPED at 50c gross, self-cleaning. Requires the
read-WRITE key (it PLACES REAL ORDERS). Run via run_smoke_test.ps1 (sets the RW env).

Exercises the three paths that were mock-tested only:
  A. W3 partial-fill cancel + W4 ack-fee — order MORE than the resting top-ask size -> a real
     partial fill -> the resting remainder is cancelled; the booked fee is reported vs the exchange.
  B. W2 flatten — a 2-leg wing where leg 2's buy is FORCED to raise -> leg 1 is flattened (sold)
     and the Book squared.

SAFETY RAILS (for an unattended/ supervised run):
  * MAX_SPEND_CENTS = 50 hard cap on cumulative GROSS buys; a planned order that would breach it
    raises before placing.
  * Refuses to touch any KXHIGHCHI ticker (the armed live bot's market).
  * CLEANUP in a finally block: market-sell every contract this run opened, then report whether the
    account holds any test ticker. Worst case if everything fails = <= 50c.
  * TEMP Book / live_log / kill_switch paths so the live bot's state files are never written.
  * Public /markets + /orderbook reads are UNauthenticated (no query-string signing pitfalls).
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd
import requests

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from weather_alpha import engine
from weather_alpha.config import MarketCfg, load_config
from weather_alpha.execution import execute
from weather_alpha.kalshi import KalshiClient, KalshiContract
from weather_alpha.positions import Book
from weather_alpha.strategy import StrategyOutput, TargetPosition

MAX_SPEND_CENTS = 50
_API = "https://api.elections.kalshi.com/trade-api/v2"
_NONCHI = [s for s in (
    "KXHIGHNY", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN", "KXHIGHPHIL", "KXHIGHLAX", "KXHIGHTLV",
    "KXHIGHTNOLA", "KXHIGHTSEA", "KXHIGHTSFO", "KXHIGHTDC", "KXHIGHTATL", "KXHIGHTMIN", "KXHIGHTPHX",
    "KXHIGHTBOS", "KXHIGHTDAL", "KXHIGHTOKC", "KXHIGHTSATX", "KXHIGHTHOU")]

_spent_cents = 0


def _pub(path: str) -> dict:
    r = requests.get(_API + path, timeout=20, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _liftable_ask(ticker: str) -> tuple[int | None, int]:
    """(yes_ask_cents, size_at_ask) by lifting the best resting NO bid. (None, 0) if no ask.

    Kalshi's /orderbook returns ONLY `orderbook_fp` with `no_dollars`/`yes_dollars` = [[price$,size],..]
    (price as a dollar string). To BUY yes you lift the highest NO bid: yes_ask = $1 - best_no_price."""
    ob = _pub(f"/markets/{ticker}/orderbook").get("orderbook_fp", {}) or {}
    no = ob.get("no_dollars") or []
    if not no:
        return None, 0
    price, size = max(no, key=lambda x: float(x[0]))     # highest NO bid = cheapest YES ask
    return round((1.0 - float(price)) * 100), int(float(size))


def discover(want: int = 3) -> list[dict]:
    """Up to `want` cheap, liftable, NON-CHI buckets: yes_ask<=5c, size>=2, ask*size<=20c."""
    out: list[dict] = []
    for s in _NONCHI:
        if len(out) >= want:
            break
        try:
            ms = _pub(f"/markets?series_ticker={s}&status=open&limit=100").get("markets", [])
        except Exception:
            continue
        for m in ms:
            tk = m["ticker"]
            if "KXHIGHCHI" in tk:
                continue
            ask, sz = _liftable_ask(tk)
            if ask and 1 <= ask <= 5 and sz >= 2 and ask * sz <= 20:
                out.append({"ticker": tk, "event": m.get("event_ticker"), "ask": ask, "size": sz})
                if len(out) >= want:
                    break
    return out


def _contract(spec: dict) -> KalshiContract:
    a = spec["ask"] / 100.0
    return KalshiContract(
        ticker=spec["ticker"], event_ticker=spec.get("event"), bucket_spec="smoke", subtitle="smoke",
        strike_type=None, strike=None,
        yes_bid=max(a - 0.02, 0.01), yes_ask=a, no_bid=max(1 - a - 0.02, 0.01), no_ask=min(1 - a + 0.02, 0.99),
        last_price=a, volume_24h=None, open_interest=None, open_time_utc=None, close_time_utc=None,
        status="active", is_live=True,
    )


def _fault_client(cfg, *, err_on_buy: str | None = None) -> KalshiClient:
    """A real RW client that tracks gross spend (50c cap) and optionally forces a BUY on
    `err_on_buy` to raise (to exercise the W2 flatten). Sells/cancels pass through untouched."""
    cli = KalshiClient(cfg.kalshi, authenticated=True)
    orig = cli.place_order

    async def place_order(*, ticker, side, action, count, limit_price_cents, client_order_id=None):
        global _spent_cents
        assert "KXHIGHCHI" not in ticker, f"refusing CHI ticker {ticker}"
        if action == "buy":
            if err_on_buy and ticker == err_on_buy:
                raise RuntimeError(f"FAULT-INJECT: forced rejection on {ticker} buy (W2 flatten test)")
            if _spent_cents + count * limit_price_cents > MAX_SPEND_CENTS:
                raise RuntimeError(f"50c CAP: {_spent_cents}c + {count*limit_price_cents}c > {MAX_SPEND_CENTS}c")
            _spent_cents += count * limit_price_cents
        try:
            return await orig(ticker=ticker, side=side, action=action, count=count,
                              limit_price_cents=limit_price_cents, client_order_id=client_order_id)
        except Exception as e:
            body = getattr(getattr(e, "response", None), "text", "")
            print(f"  !! place_order {action} {ticker} x{count}@{limit_price_cents}c FAILED: "
                  f"{type(e).__name__} {str(body)[:220]}", flush=True)
            raise

    cli.place_order = place_order  # type: ignore
    return cli


async def _held(client: KalshiClient, ticker: str, side: str) -> int:
    for p in await client.get_positions():
        if p.ticker == ticker and p.side == side:
            return p.contracts
    return 0


async def _sell_to_flat(client: KalshiClient, ticker: str, side: str) -> None:
    held = await _held(client, ticker, side)
    if held <= 0:
        return
    await client.place_order(ticker=ticker, side=side, action="sell", count=held, limit_price_cents=1,
                             client_order_id=f"smoke-cleanup-{ticker}-{int(time.time()*1000)}")
    await asyncio.sleep(1.2)
    print(f"  cleanup: sold {held} {side} {ticker}; now held {await _held(client, ticker, side)}", flush=True)


async def main() -> int:
    cfg = load_config(str(_ROOT / "config" / "live.yaml"), require_live_creds=True)
    if not cfg.is_live():
        sys.exit("config is not LIVE")
    tmp = Path(tempfile.mkdtemp(prefix="smoke_"))
    cfg = dataclasses.replace(cfg, paths=dataclasses.replace(
        cfg.paths, positions_snapshot=str(tmp / "pos.json"), live_log=str(tmp / "log.parquet"),
        kill_switch=str(tmp / "NO_KILL")))
    client = KalshiClient(cfg.kalshi, authenticated=True)        # plain RW client for reads + cleanup
    bal = await client.get_balance()
    print(f"== LIVE SMOKE TEST == balance ${bal/100:.2f} | cap {MAX_SPEND_CENTS}c | tmp {tmp}", flush=True)
    pred = engine._placeholder_prediction(cfg, pd.Timestamp.now(tz=cfg.local_tz).normalize().tz_localize(None))
    mkt = MarketCfg(name="SMOKE", event_pattern="SMOKE", station="SMOKE", local_tz=cfg.local_tz)
    results: dict[str, str] = {}
    test_tickers: set[tuple[str, str]] = set()
    try:
        specs = discover(want=3)
        print(f"discovered {len(specs)} cheap liftable non-CHI buckets: "
              f"{[(s['ticker'], s['ask'], s['size']) for s in specs]}", flush=True)
        if not specs:
            print("!! no liquidity — abort (nothing to fill against)", flush=True)
            return 2

        # ---- TEST A: partial fill (target = top-ask size + 2) -> W3 cancel + W4 fee ----
        a = specs[0]
        ca = _contract(a)
        test_tickers.add((ca.ticker, "yes"))
        tg = [TargetPosition(ticker=ca.ticker, side="yes", target_contracts=a["size"] + 2,
                             limit_price_cents=a["ask"], bucket_spec="smoke")]
        cli_a = _fault_client(cfg)
        book = Book()
        await execute(cfg, pred, [ca], StrategyOutput(targets=tg, diagnostics={}), book, cli_a,
                      market=mkt, bankroll_usd=bal / 100.0)
        booked = sum(p.contracts for p in book.positions.values())
        exch = await _held(client, ca.ticker, "yes")
        results["A partial fill (W3)"] = f"{'PASS' if 0 < booked < a['size']+2 else 'FAIL'} booked {booked}/{a['size']+2}"
        results["A remainder cancelled (W3)"] = f"{'PASS' if exch == booked else 'FAIL'} exchange holds {exch} (== booked)"
        results["A fee booked (W4)"] = f"{book.fees_paid_cents}c on {booked}@{a['ask']}c"
        await cli_a.close()

        # ---- TEST B: flatten on a forced leg-2 error (needs 2 FRESH buckets) -> W2 ----
        fresh = [s for s in specs if (s["ticker"], "yes") not in test_tickers]
        tails = [s for s in fresh if "-T" in s["ticker"]]
        if len(fresh) < 2:
            results["B flatten (W2)"] = "SKIP (need 2 fresh buckets; not enough liquidity)"
        else:
            # leg1 must FILL for the flatten to have something to flatten; prefer a TAIL bucket
            # (middle 'B' buckets can reject a marketable buy with invalid_parameters). leg2 is
            # fault-injected (raises BEFORE placing), so its bucket validity is irrelevant.
            l1 = (tails or fresh)[0]
            l2 = next(s for s in fresh if s["ticker"] != l1["ticker"])
            c1, c2 = _contract(l1), _contract(l2)
            test_tickers.update({(c1.ticker, "yes"), (c2.ticker, "yes")})
            tg2 = [TargetPosition(ticker=c1.ticker, side="yes", target_contracts=min(l1["size"], 2),
                                  limit_price_cents=l1["ask"], bucket_spec="smoke"),
                   TargetPosition(ticker=c2.ticker, side="yes", target_contracts=min(l2["size"], 2),
                                  limit_price_cents=l2["ask"], bucket_spec="smoke")]
            cli_b = _fault_client(cfg, err_on_buy=c2.ticker)
            book2 = Book()
            r2 = await execute(cfg, pred, [c1, c2], StrategyOutput(targets=tg2, diagnostics={}), book2, cli_b,
                               market=mkt, bankroll_usd=bal / 100.0)
            await asyncio.sleep(1.5)
            l1_exch = await _held(client, c1.ticker, "yes")
            ok = (r2.diagnostics.get("halt_reason") == "leg_error" and l1_exch == 0
                  and book2.open_for(c1.ticker, "yes") is None)
            results["B flatten leg-1 after leg-2 error (W2)"] = (
                f"{'PASS' if ok else 'FAIL'} halt={r2.diagnostics.get('halt_reason')} "
                f"exch_leg1={l1_exch} book_flat={book2.open_for(c1.ticker, 'yes') is None}")
            await cli_b.close()
        return 0
    finally:
        print("\n-- cleanup (sell every test ticker to flat) --", flush=True)
        for tk, side in sorted(test_tickers):
            try:
                await _sell_to_flat(client, tk, side)
            except Exception as e:
                print(f"  !! cleanup FAILED for {tk} {side}: {e!r} — MANUAL CHECK NEEDED", flush=True)
        await asyncio.sleep(1.0)
        leftover = [(p.ticker, p.side, p.contracts) for p in await client.get_positions()
                    if (p.ticker, p.side) in test_tickers and p.contracts]
        print("\n================ SMOKE RESULT ================", flush=True)
        for k, v in results.items():
            print(f"  {k}: {v}", flush=True)
        print(f"  gross spent ~{_spent_cents}c (cap {MAX_SPEND_CENTS}c)", flush=True)
        print(f"  FLAT on test tickers: {'YES' if not leftover else 'NO -> ' + str(leftover) + '  *** MANUAL ***'}", flush=True)
        print("=============================================", flush=True)
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
