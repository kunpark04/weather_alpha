"""DRY-RUN: preview today's strategy decision against the LIVE Kalshi market.

Places NO orders and writes NO state. It reads the live market + real balance, runs
the SAME strategy dispatch the engine uses (engine._dispatch_strategy), and prints the
orders it WOULD submit at the current book. It never calls execute()/place_order and
never touches the Book or live_log — safe to run any time, including with the
read-write key.

This is the correct way to preview a LIVE decision: `python -m weather_alpha --headless`
in `mode: live` would *actually place* orders, not preview them.

Usage (PowerShell):
    $env:KALSHI_KEY_ID = (Get-Content path\to\readonly-key-id -Raw).Trim()
    $env:KALSHI_PRIVATE_KEY_PATH = "path\to\readonly-private-key.pem"
    python scripts/dry_run.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from weather_alpha.config import load_config  # noqa: E402
from weather_alpha.kalshi import KalshiClient  # noqa: E402
from weather_alpha.engine import (  # noqa: E402
    _dispatch_strategy, _placeholder_prediction, _tradeable_contracts,
)


async def _amain() -> int:
    cfg = load_config()
    anchor = pd.Timestamp.now(tz=cfg.local_tz).normalize().tz_localize(None)
    now_ct = pd.Timestamp.now(tz=cfg.local_tz)

    async with KalshiClient(cfg.kalshi, authenticated=True) as client:
        bal_cents = await client.get_balance()
        contracts = await client.fetch_event(anchor, cfg.execution.market_event_pattern)

    live, skip = _tradeable_contracts(contracts, anchor)
    w = cfg.strategy.wing

    print("=" * 70)
    print(f"DRY-RUN (no orders placed)  event={anchor.date()}  now={now_ct:%Y-%m-%d %H:%M %Z}")
    print("=" * 70)
    print(f"real balance (get_balance) : ${bal_cents / 100:,.2f}   [live would size on this]")
    print(f"strategy                   : {cfg.strategy.name}  anchor={w.anchor}  flat_usd=${w.flat_usd}  "
          f"max_ask_sum={w.max_ask_sum}  assumed_win_prob={w.assumed_win_prob}  fee_aware={w.fee_aware}")
    print(f"today's event              : {len(contracts)} contracts ({len(live)} live)")
    for c in sorted(contracts, key=lambda x: x.bucket_spec):
        print(f"    {c.bucket_spec:>7}  yes_ask={c.yes_ask:.2f}  no_ask={c.no_ask:.2f}  live={c.is_live}  {c.ticker}")

    if skip is not None:
        print(f"\nEVENT GUARD: {skip}\n--> would place NO orders this cycle.")
        return 0

    pred = _placeholder_prediction(cfg, anchor)
    strat = _dispatch_strategy(cfg, pred, contracts, None, max(0.0, bal_cents / 100.0))

    print(f"\nstrategy diagnostics       : {strat.diagnostics}")
    if not strat.targets:
        print("\n--> would place NO orders (strategy emitted no targets; see diagnostics).")
        return 0

    print(f"\nWOULD PLACE {len(strat.targets)} order(s):")
    total = 0
    for t in strat.targets:
        cost = t.target_contracts * t.limit_price_cents
        total += cost
        print(f"    BUY {t.side.upper():3} {t.bucket_spec:>7}  x{t.target_contracts:<4} @ {t.limit_price_cents:>2}c"
              f"  = ${cost / 100:5.2f}   {t.ticker}")
    print(f"    {'total outlay':>38} = ${total / 100:.2f}")
    print("\n(NO orders placed, NO state written. Live fires at the 1 PM CT anchor.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
