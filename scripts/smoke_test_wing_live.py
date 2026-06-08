"""Option-2 BONUS — the most faithful off-anchor test of the bot's real order path, on CONTESTED
(tradeable, mid-priced) NON-CHI buckets (NOT the cheap far/decided buckets that returned
invalid_parameters):

  1. ACCEPTANCE  — a resting 1c buy on several contested buckets (free, cancelled) confirms they
                   accept orders at all (the structural test the far bucket failed).
  2. WING/FLATTEN — a real 2-leg wing where leg1 (contested) FILLS and leg2 is fault-injected to
                    raise -> leg1 is flattened (W2). Validated only if leg1 truly filled (r.fills>=1).

Same execute()->place_order code path as the live bot. HARD-CAPPED 50c gross, self-cleaning, temp state.
Run via the RW key.
"""
from __future__ import annotations

import asyncio
import dataclasses
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

import scripts.smoke_test_live as st          # reuse _pub/_liftable_ask/_contract/_fault_client/_held/_sell_to_flat
from weather_alpha import engine
from weather_alpha.config import MarketCfg, load_config
from weather_alpha.execution import execute
from weather_alpha.kalshi import KalshiClient
from weather_alpha.positions import Book
from weather_alpha.strategy import StrategyOutput, TargetPosition


def discover_contested(lo=8, hi=30, want=6) -> list[dict]:
    """Tradeable (not far/decided) NON-CHI buckets: liftable yes_ask in [lo,hi]c, size>=2."""
    out = []
    for s in st._NONCHI:
        if len(out) >= want:
            break
        try:
            ms = st._pub(f"/markets?series_ticker={s}&status=open&limit=100").get("markets", [])
        except Exception:
            continue
        for m in ms:
            tk = m["ticker"]
            if "KXHIGHCHI" in tk:
                continue
            ask, sz = st._liftable_ask(tk)
            if ask and lo <= ask <= hi and sz >= 2:
                out.append({"ticker": tk, "event": m.get("event_ticker"), "ask": ask, "size": sz})
                if len(out) >= want:
                    break
    return sorted(out, key=lambda c: c["ask"])      # cheapest first (used for the wing legs -> least $)


async def accept(cli: KalshiClient, ticker: str) -> str:
    """Resting 1c (non-crossing -> no fill, free) buy to test ORDER ACCEPTANCE; cancel it."""
    try:
        ack = await cli.place_order(ticker=ticker, side="yes", action="buy", count=1, limit_price_cents=1,
                                    client_order_id=f"acc-{ticker}-{int(time.time()*1000)}")
        oid = ack.get("order_id") or (ack.get("order") or {}).get("order_id")
        if oid:
            await cli.cancel_order(oid)
        return "ACCEPTED"
    except Exception as e:
        return f"REJECTED {str(getattr(getattr(e, 'response', None), 'text', ''))[:80]}"


async def main() -> int:
    cfg = load_config(str(_ROOT / "config" / "live.yaml"), require_live_creds=True)
    tmp = Path(tempfile.mkdtemp(prefix="smokewing_"))
    cfg = dataclasses.replace(cfg, paths=dataclasses.replace(
        cfg.paths, positions_snapshot=str(tmp / "pos.json"), live_log=str(tmp / "log.parquet"),
        kill_switch=str(tmp / "NO_KILL")))
    client = KalshiClient(cfg.kalshi, authenticated=True)
    bal = await client.get_balance()
    print(f"== CONTESTED-WING SMOKE == balance ${bal/100:.2f} | cap {st.MAX_SPEND_CENTS}c", flush=True)
    cont = discover_contested(want=6)
    print(f"contested buckets (ask 8-30c, size>=2): {[(c['ticker'], c['ask'], c['size']) for c in cont]}", flush=True)
    if len(cont) < 2:
        print("!! not enough contested buckets right now"); await client.close(); return 2
    pred = engine._placeholder_prediction(cfg, pd.Timestamp.now(tz=cfg.local_tz).normalize().tz_localize(None))
    mkt = MarketCfg(name="SMOKE", event_pattern="SMOKE", station="SMOKE", local_tz=cfg.local_tz)
    results: dict[str, str] = {}
    test_tickers: set[tuple[str, str]] = set()
    try:
        # 1. ACCEPTANCE — resting 1c on several contested buckets (free)
        print("\n-- order ACCEPTANCE on contested buckets (resting 1c, cancelled) --", flush=True)
        acc = []
        for c in cont[:4]:
            r = await accept(client, c["ticker"])
            acc.append(r)
            print(f"  {c['ticker']:<26} ask={c['ask']:>2}c -> {r}", flush=True)
        results["contested order acceptance"] = f"{sum(r=='ACCEPTED' for r in acc)}/{len(acc)} accepted"

        # 2. WING / FLATTEN — leg1 (contested, cheapest) FILLS, leg2 fault-injected -> flatten leg1
        l1, l2 = cont[0], cont[1]
        c1, c2 = st._contract(l1), st._contract(l2)
        test_tickers.update({(c1.ticker, "yes"), (c2.ticker, "yes")})
        tg = [TargetPosition(ticker=c1.ticker, side="yes", target_contracts=min(l1["size"], 2),
                             limit_price_cents=l1["ask"], bucket_spec="smoke"),
              TargetPosition(ticker=c2.ticker, side="yes", target_contracts=min(l2["size"], 2),
                             limit_price_cents=l2["ask"], bucket_spec="smoke")]
        cli_w = st._fault_client(cfg, err_on_buy=c2.ticker)
        book = Book()
        print(f"\n-- WING: leg1={c1.ticker}@{l1['ask']}c (should FILL) | leg2={c2.ticker} (fault-inject) --", flush=True)
        r2 = await execute(cfg, pred, [c1, c2], StrategyOutput(targets=tg, diagnostics={}), book, cli_w,
                           market=mkt, bankroll_usd=bal / 100.0)
        await asyncio.sleep(1.5)
        l1_exch = await st._held(client, c1.ticker, "yes")
        leg1_filled = r2.fills >= 1                                   # genuine: leg1 actually filled
        flat_ok = (r2.diagnostics.get("halt_reason") == "leg_error" and l1_exch == 0
                   and book.open_for(c1.ticker, "yes") is None)
        results["leg1 marketable order (contested) accepted+filled"] = "YES" if leg1_filled else "NO (rejected/no fill)"
        results["W2 flatten leg-1 after leg-2 error"] = (
            f"{'PASS' if (leg1_filled and flat_ok) else ('VACUOUS (leg1 never filled)' if not leg1_filled else 'FAIL')} "
            f"fills={r2.fills} exch_leg1={l1_exch} book_flat={book.open_for(c1.ticker, 'yes') is None}")
        await cli_w.close()
        return 0
    finally:
        print("\n-- cleanup --", flush=True)
        for tk, side in sorted(test_tickers):
            try:
                await st._sell_to_flat(client, tk, side)
            except Exception as e:
                print(f"  !! cleanup FAILED {tk} {side}: {e!r} — MANUAL CHECK", flush=True)
        await asyncio.sleep(1)
        leftover = [(p.ticker, p.side, p.contracts) for p in await client.get_positions()
                    if (p.ticker, p.side) in test_tickers and p.contracts]
        print("\n========== CONTESTED-WING RESULT ==========", flush=True)
        for k, v in results.items():
            print(f"  {k}: {v}", flush=True)
        print(f"  gross spent ~{st._spent_cents}c (cap {st.MAX_SPEND_CENTS}c)", flush=True)
        print(f"  FLAT on test tickers: {'YES' if not leftover else 'NO -> ' + str(leftover) + ' *** MANUAL ***'}", flush=True)
        print(f"  balance now: ${await client.get_balance()/100:.2f}", flush=True)
        print("===========================================", flush=True)
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
