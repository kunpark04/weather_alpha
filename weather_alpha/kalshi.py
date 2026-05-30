"""Kalshi REST client — public market data (no auth) + authenticated order/portfolio
endpoints (RSA-PSS signing). Async via httpx.

Public methods used in PAPER mode:
    - fetch_event(date) -> list[KalshiContract]
Authenticated methods used in LIVE mode:
    - get_balance() -> int (cents)
    - get_positions() -> list[KalshiPosition]
    - place_order(...) -> dict
    - cancel_order(order_id) -> dict
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import pandas as pd
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from weather_alpha.config import KalshiCfg
from weather_alpha.pmf import parse_kalshi_subtitle

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KalshiContract:
    ticker: str
    event_ticker: str
    bucket_spec: str                  # parseable by pmf.parse_bucket
    subtitle: str
    strike_type: str | None
    strike: float | None
    yes_bid: float                    # dollars
    yes_ask: float
    no_bid: float
    no_ask: float
    last_price: float
    volume_24h: float | None
    open_interest: float | None
    open_time_utc: pd.Timestamp | None
    close_time_utc: pd.Timestamp | None
    status: str
    is_live: bool

    @property
    def yes_mid(self) -> float:
        return (self.yes_bid + self.yes_ask) / 2.0

    @property
    def no_mid(self) -> float:
        return (self.no_bid + self.no_ask) / 2.0


@dataclass(frozen=True)
class KalshiPosition:
    ticker: str
    side: str                         # "yes" | "no"
    contracts: int
    avg_price_cents: int


# ---------------------------------------------------------------------------
# Signing (LIVE mode only)
# ---------------------------------------------------------------------------

def _load_private_key(path: Path) -> rsa.RSAPrivateKey:
    pem = Path(path).read_bytes()
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError(f"Expected RSA private key at {path}, got {type(key).__name__}")
    return key


def _sign_request(key: rsa.RSAPrivateKey, timestamp_ms: int, method: str, path: str) -> str:
    """Kalshi RSA-PSS signature: sign `timestamp_ms + method + path` with SHA256, return base64."""
    msg = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")
    sig = key.sign(
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("ascii")


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class KalshiClient:
    def __init__(self, cfg: KalshiCfg, *, authenticated: bool):
        self._cfg = cfg
        self._authenticated = authenticated
        self._http = httpx.AsyncClient(
            base_url=cfg.api_base,
            timeout=cfg.request_timeout_seconds,
            headers={"User-Agent": "weather-alpha/0.1"},
        )
        if authenticated:
            key_id = os.environ.get(cfg.key_id_env, "")
            key_path = os.environ.get(cfg.private_key_path_env, "")
            if not key_id or not key_path:
                raise RuntimeError(
                    f"Authenticated client requires ${cfg.key_id_env} + ${cfg.private_key_path_env}"
                )
            self._key_id = key_id
            self._private_key = _load_private_key(Path(key_path))
        else:
            self._key_id = ""
            self._private_key = None

    async def close(self) -> None:
        await self._http.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    # ---- public endpoints -----------------------------------------------------

    async def fetch_event(self, settlement_date: pd.Timestamp,
                          event_pattern: str = "KXHIGHCHI") -> list[KalshiContract]:
        """Fetch the contracts for one settlement date's event."""
        ticker = f"{event_pattern}-{pd.Timestamp(settlement_date).strftime('%y%b%d').upper()}"
        r = await self._http.get("/markets", params={"event_ticker": ticker})
        r.raise_for_status()
        markets = r.json().get("markets", [])
        contracts: list[KalshiContract] = []
        for m in markets:
            spec = _spec_from_market(m)
            status = (m.get("status") or "").lower()
            contracts.append(KalshiContract(
                ticker=m["ticker"],
                event_ticker=ticker,
                bucket_spec=spec,
                subtitle=m.get("subtitle") or "",
                strike_type=m.get("strike_type"),
                strike=m.get("floor_strike"),
                yes_bid=float(m.get("yes_bid_dollars", 0)),
                yes_ask=float(m.get("yes_ask_dollars", 1)),
                no_bid=float(m.get("no_bid_dollars", 0)),
                no_ask=float(m.get("no_ask_dollars", 1)),
                last_price=float(m.get("last_price_dollars", 0)),
                volume_24h=m.get("volume_24h_fp"),
                open_interest=m.get("open_interest_fp"),
                open_time_utc=pd.to_datetime(m.get("open_time"), utc=True, errors="coerce"),
                close_time_utc=pd.to_datetime(m.get("close_time"), utc=True, errors="coerce"),
                status=status,
                is_live=status in ("open", "active"),
            ))
        contracts.sort(key=_contract_sort_key)
        return contracts

    # ---- authenticated endpoints ----------------------------------------------

    async def get_balance(self) -> int:
        """Account balance in cents."""
        data = await self._signed_request("GET", "/portfolio/balance")
        return int(data.get("balance", 0))

    async def get_positions(self) -> list[KalshiPosition]:
        data = await self._signed_request("GET", "/portfolio/positions")
        out: list[KalshiPosition] = []
        for p in data.get("market_positions", []):
            qty = int(p.get("position", 0))
            if qty == 0:
                continue
            side = "yes" if qty > 0 else "no"
            out.append(KalshiPosition(
                ticker=p["ticker"],
                side=side,
                contracts=abs(qty),
                avg_price_cents=int(p.get("market_exposure", 0) / max(abs(qty), 1)),
            ))
        return out

    async def place_order(
        self,
        ticker: str,
        side: str,                         # "yes" | "no"
        action: str,                       # "buy" | "sell"
        count: int,
        limit_price_cents: int,
        client_order_id: str | None = None,
    ) -> dict[str, Any]:
        if side not in ("yes", "no") or action not in ("buy", "sell"):
            raise ValueError(f"bad side/action: {side!r}/{action!r}")
        if count <= 0:
            raise ValueError(f"count must be positive: {count}")
        payload = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": int(count),
            "type": "limit",
            "yes_price": int(limit_price_cents) if side == "yes" else None,
            "no_price":  int(limit_price_cents) if side == "no"  else None,
            "client_order_id": client_order_id or f"wa-{int(time.time() * 1000)}",
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        logger.info("Kalshi place_order %s %s %s x %d @ %d¢", action, side, ticker,
                    count, limit_price_cents)
        return await self._signed_request("POST", "/portfolio/orders", json=payload)

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        logger.info("Kalshi cancel_order %s", order_id)
        return await self._signed_request("DELETE", f"/portfolio/orders/{order_id}")

    # ---- signing wrapper ------------------------------------------------------

    async def _signed_request(self, method: str, path: str, *, json: Any = None,
                              max_attempts: int = 3) -> dict[str, Any]:
        if not self._authenticated or self._private_key is None:
            raise RuntimeError("client is not authenticated")
        last: Exception | None = None
        for attempt in range(max_attempts):
            ts = int(time.time() * 1000)
            sig = _sign_request(self._private_key, ts, method, path)
            headers = {
                "KALSHI-ACCESS-KEY":        self._key_id,
                "KALSHI-ACCESS-SIGNATURE":  sig,
                "KALSHI-ACCESS-TIMESTAMP":  str(ts),
                "Accept":                   "application/json",
            }
            try:
                r = await self._http.request(method, path, json=json, headers=headers)
                if r.status_code >= 500 and attempt + 1 < max_attempts:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                r.raise_for_status()
                return r.json() if r.content else {}
            except (httpx.TransportError, httpx.HTTPStatusError) as e:
                last = e
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                raise
        assert last is not None
        raise last


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec_from_market(m: dict[str, Any]) -> str:
    """Convert a Kalshi market dict to a bucket_spec (subtitle-first, strike fallback)."""
    spec = parse_kalshi_subtitle(m.get("subtitle"))
    if spec is not None:
        return spec
    st = m.get("strike_type")
    strike = m.get("floor_strike")
    if st == "less" and strike is not None:
        return f"<{int(strike)}"
    if st == "greater" and strike is not None:
        return f">{int(strike)}"
    raise ValueError(f"Cannot parse Kalshi market: {m.get('ticker')!r} subtitle={m.get('subtitle')!r}")


def _contract_sort_key(c: KalshiContract) -> int:
    if c.strike_type == "less":
        return -1
    if c.strike_type == "greater":
        return 999
    try:
        return int(c.subtitle.split("°")[0])
    except Exception:
        return 500


def contracts_to_dataframe(contracts: list[KalshiContract]) -> pd.DataFrame:
    """For display + logging. One row per contract."""
    return pd.DataFrame([{
        "ticker":         c.ticker,
        "bucket_spec":    c.bucket_spec,
        "subtitle":       c.subtitle,
        "status":         c.status,
        "is_live":        c.is_live,
        "yes_bid":        c.yes_bid,
        "yes_ask":        c.yes_ask,
        "no_bid":         c.no_bid,
        "no_ask":         c.no_ask,
        "yes_mid":        c.yes_mid,
        "no_mid":         c.no_mid,
        "last_price":     c.last_price,
        "volume_24h":     c.volume_24h,
        "open_interest":  c.open_interest,
        "open_time_utc":  c.open_time_utc,
        "close_time_utc": c.close_time_utc,
    } for c in contracts])
