"""Read-only Kalshi API connectivity check.

Proves the authenticated path works (RSA-PSS signing + credentials) WITHOUT
placing, modifying, or cancelling any orders. Calls only:
    - fetch_event()    GET /markets             (public; sanity check)
    - get_balance()    GET /portfolio/balance   (authenticated)
    - get_positions()  GET /portfolio/positions (authenticated)

This script CANNOT trade: it never imports or calls place_order. Safe to run
against the live account while still in paper mode, before flipping mode: live.

Usage (PowerShell):
    $env:KALSHI_KEY_ID = "<your-access-key-id>"
    $env:KALSHI_PRIVATE_KEY_PATH = "weather_alpha/kalshi.pem"
    python scripts/check_kalshi_auth.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from weather_alpha.config import load_config  # noqa: E402
from weather_alpha.kalshi import KalshiClient  # noqa: E402


async def _amain() -> int:
    cfg = load_config()
    key_id = os.environ.get(cfg.kalshi.key_id_env, "").strip()
    key_path = os.environ.get(cfg.kalshi.private_key_path_env, "").strip()

    print("=" * 60)
    print("Kalshi authenticated connectivity check (READ-ONLY)")
    print("=" * 60)
    print(f"API base                 : {cfg.kalshi.api_base}")
    print(f"${cfg.kalshi.key_id_env:<24}: {'set' if key_id else 'MISSING'}")
    print(f"${cfg.kalshi.private_key_path_env:<24}: {key_path or 'MISSING'}")
    if not key_id or not key_path:
        print("\nFAIL: both env vars must be set. See the usage header.")
        return 2
    if not Path(key_path).exists():
        print(f"\nFAIL: private key file not found at {key_path}")
        return 2

    client = KalshiClient(cfg.kalshi, authenticated=True)
    try:
        today = pd.Timestamp.now(tz=cfg.local_tz).normalize().tz_localize(None)
        contracts = await client.fetch_event(today, cfg.execution.market_event_pattern)
        live = [c for c in contracts if c.is_live]
        print(f"\n[public]  /markets today  : {len(contracts)} contracts ({len(live)} live)")

        balance_cents = await client.get_balance()
        print(f"[auth]    /portfolio/balance: ${balance_cents / 100:,.2f}")

        positions = await client.get_positions()
        print(f"[auth]    /portfolio/positions: {len(positions)} open")
        for p in positions:
            print(f"            {p.ticker}  {p.side}  x{p.contracts}  @ {p.avg_price_cents}¢")

        print("\nPASS: authenticated connection works. No orders were placed.")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"\nFAIL: {type(e).__name__}: {str(e)[:300]}")
        print("  Common causes: wrong KALSHI_KEY_ID, public key not uploaded to "
              "Kalshi, key/id mismatch, or clock skew on the signature timestamp.")
        return 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))
