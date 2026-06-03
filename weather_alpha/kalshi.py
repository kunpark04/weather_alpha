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
from urllib.parse import urlsplit

import httpx
import pandas as pd
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from weather_alpha.config import KalshiCfg
from weather_alpha.pmf import parse_kalshi_subtitle

logger = logging.getLogger(__name__)

_MONTH_ABBR = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def event_date_code(settlement_date) -> str:
    """Kalshi event-ticker date code, e.g. '26JUN01' — LOCALE-INDEPENDENT (I1). strftime '%b'
    is locale-dependent (a non-English LC_TIME yields a wrong month abbrev → wrong ticker);
    year/day are numeric so strftime is safe there, only the month abbrev is hardcoded."""
    ts = pd.Timestamp(settlement_date)
    return f"{ts.strftime('%y')}{_MONTH_ABBR[ts.month - 1]}{ts.strftime('%d')}"


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
    # W4: exchange-truth lifetime figures for this market position (cents). Both are REQUIRED
    # fields on the /portfolio/positions MarketPosition (realized_pnl_dollars, fees_paid_dollars);
    # default 0 keeps non-API constructors (tests / legacy callers) working.
    realized_pnl_cents: int = 0
    fees_paid_cents: int = 0


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


# Order-rate-throttle hardening: Kalshi rejects orders placed too rapidly in a burst with HTTP 400
# code=invalid_parameters (observed in live smoke testing). The SAME code is also used for a
# genuinely malformed order, so we can't distinguish them — we retry a FEW times with backoff then
# give up: a transient throttle clears on the backoff, a truly bad order just fails after the
# retries (a few seconds later). A 400 means the order was NOT created, so re-sending the same
# client_order_id across retries cannot double-fill.
_ORDER_RATE_RETRIES = 3
_ORDER_RATE_BACKOFF_S = 0.5


def _is_rate_throttle(resp) -> bool:
    """True if a 400 response is Kalshi's order-rate throttle (code=invalid_parameters)."""
    try:
        return (resp.json().get("error") or {}).get("code") == "invalid_parameters"
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class KalshiClient:
    def __init__(self, cfg: KalshiCfg, *, authenticated: bool):
        self._cfg = cfg
        self._authenticated = authenticated
        # Kalshi signs the FULL request path, including the prefix baked into api_base
        # (e.g. "/trade-api/v2"). httpx prepends that prefix to the actual request URL,
        # so the signed string must include it too — otherwise every authenticated call
        # is signed over the wrong bytes and rejected with 401.
        self._base_path = urlsplit(cfg.api_base).path.rstrip("/")
        # W6: validate + load credentials BEFORE opening the HTTP client, so a bad/missing key
        # raises without leaking an already-opened client (no __aexit__ would run to close it).
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
        self._http = httpx.AsyncClient(
            base_url=cfg.api_base,
            timeout=cfg.request_timeout_seconds,
            headers={"User-Agent": "weather-alpha/0.1"},
        )

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
        ticker = f"{event_pattern}-{event_date_code(settlement_date)}"
        r = await self._http.get("/markets", params={"event_ticker": ticker})
        r.raise_for_status()
        markets = r.json().get("markets", [])
        contracts: list[KalshiContract] = []
        for m in markets:
            try:
                spec = _spec_from_market(m)
            except ValueError as e:
                # One unparseable contract must not sink the whole event (nor, in a
                # multi-market process, the other cities). Drop it and keep going.
                logger.warning("skipping unparseable market: %s", e)
                continue
            status = (m.get("status") or "").lower()
            contracts.append(KalshiContract(
                ticker=m["ticker"],
                # #7: event_ticker from the API's OWN data (its field, else derived from the market
                # ticker) — not the ticker we requested — so the event-date guard checks real
                # response data and can actually catch a wrong/stale event.
                event_ticker=(m.get("event_ticker") or "-".join(str(m.get("ticker", "")).split("-")[:2]) or ticker),
                bucket_spec=spec,
                subtitle=m.get("subtitle") or m.get("yes_sub_title") or "",
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
        return _parse_balance_cents(data)

    async def get_positions(self) -> list[KalshiPosition]:
        data = await self._signed_request("GET", "/portfolio/positions")
        return _parse_positions(data)

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
        for attempt in range(_ORDER_RATE_RETRIES):
            try:
                return await self._signed_request("POST", "/portfolio/orders", json=payload)
            except httpx.HTTPStatusError as e:
                if (e.response.status_code == 400 and attempt + 1 < _ORDER_RATE_RETRIES
                        and _is_rate_throttle(e.response)):
                    logger.warning("place_order %s: 400 invalid_parameters (rate throttle?) — "
                                   "retry %d/%d after backoff", ticker, attempt + 1, _ORDER_RATE_RETRIES)
                    await asyncio.sleep(_ORDER_RATE_BACKOFF_S * (2 ** attempt))
                    continue
                raise
        raise RuntimeError("unreachable")  # loop always returns or raises within the retry budget

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
            sig = _sign_request(self._private_key, ts, method, self._base_path + path)
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

def _fp(value: Any) -> float:
    """Parse a Kalshi fixed-point field (a string like '12.34', or a number) to float.
    Returns 0.0 for None / unparseable input."""
    if value is None:
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_balance_cents(data: dict[str, Any]) -> int:
    """Account balance in integer cents from a /portfolio/balance response.

    The current API returns `balance_dollars` (fixed-point dollar string); the legacy
    integer `balance` (cents) was scheduled for removal on 2026-03-12. Prefer the dollar
    field and fall back to the legacy cents field only when it is the sole one present.
    """
    if data.get("balance_dollars") is not None:
        return int(round(_fp(data["balance_dollars"]) * 100))
    return int(data.get("balance", 0) or 0)


def _parse_positions(data: dict[str, Any]) -> list[KalshiPosition]:
    """Held market positions from a /portfolio/positions response.

    The current API uses `position_fp` (signed fixed-point count; +YES / -NO) and
    `market_exposure_dollars` (fixed-point dollars). The legacy integer `position` and
    `market_exposure` (cents) were scheduled for removal on 2026-03-12. Prefer the new
    fields and fall back to the legacy pair only when the new ones are absent.

    W4: also surfaces the exchange-truth `realized_pnl_dollars` + `fees_paid_dollars` (both
    REQUIRED MarketPosition fields) as cents, so a Book↔exchange reconciliation can prefer the
    real figures over the bot's per-fill fee estimate. Missing → 0 (legacy payload / cents-only).
    """
    out: list[KalshiPosition] = []
    for p in data.get("market_positions", []):
        if p.get("position_fp") is not None:
            qty = int(round(_fp(p["position_fp"])))
            exposure_cents = int(round(_fp(p.get("market_exposure_dollars")) * 100))
        else:
            qty = int(p.get("position", 0) or 0)
            exposure_cents = int(p.get("market_exposure", 0) or 0)
        if qty == 0:
            continue
        out.append(KalshiPosition(
            ticker=p["ticker"],
            side="yes" if qty > 0 else "no",
            contracts=abs(qty),
            avg_price_cents=int(exposure_cents / max(abs(qty), 1)),
            realized_pnl_cents=int(round(_fp(p.get("realized_pnl_dollars")) * 100)),
            fees_paid_cents=int(round(_fp(p.get("fees_paid_dollars")) * 100)),
        ))
    return out


def order_ack_fee_cents(ack: dict[str, Any]) -> tuple[int | None, int | None]:
    """W4: the exchange-reported (fee_cents, fill_count) from a place_order CreateOrderResponse.

    The order is nested under `order`; per the verified OpenAPI spec it carries
    `taker_fees_dollars` + `maker_fees_dollars` (total charged fee = their sum) and
    `fill_count_fp` (contracts filled at ack time). Returns (None, None) when the ack lacks the
    fee fields (e.g. a resting order, the mock test client, or a schema we don't recognize) so
    the caller falls back to the trade_fee_cents estimate. The fill_count lets the caller use
    the ack fee ONLY when it corresponds to the same quantity the position poll confirmed."""
    order = ack.get("order") if isinstance(ack.get("order"), dict) else ack
    taker = order.get("taker_fees_dollars")
    maker = order.get("maker_fees_dollars")
    if taker is None and maker is None:
        return None, None
    fee_cents = int(round((_fp(taker) + _fp(maker)) * 100))
    fc = order.get("fill_count_fp")
    fill_count = int(round(_fp(fc))) if fc is not None else None
    return fee_cents, fill_count


def _spec_from_market(m: dict[str, Any]) -> str:
    """Convert a Kalshi market dict to a bucket_spec.

    Range-text first — `subtitle` OR `yes_sub_title`: some series (e.g. KXHIGHTHOU) leave
    `subtitle` null and carry the human range only in `yes_sub_title`, so we read whichever
    is present. Then a numeric-strike fallback: `between` uses floor/cap, and the `less`/
    `greater` tails use `cap_strike`/`floor_strike` respectively (Kalshi's strike boundaries
    sit on the half-degree, so a `less` cap of 89 means "≤ 88").
    """
    spec = parse_kalshi_subtitle(m.get("subtitle") or m.get("yes_sub_title"))
    if spec is not None:
        return spec
    st = m.get("strike_type")
    floor = m.get("floor_strike")
    cap = m.get("cap_strike")
    if st == "between" and floor is not None and cap is not None:
        return f"{int(floor)}-{int(cap)}"
    if st == "less" and cap is not None:
        return f"<={int(cap) - 1}"
    if st == "greater" and floor is not None:
        return f">={int(floor) + 1}"
    raise ValueError(f"Cannot parse Kalshi market: {m.get('ticker')!r} "
                     f"subtitle={m.get('subtitle')!r} yes_sub_title={m.get('yes_sub_title')!r} "
                     f"strike_type={st!r} floor={floor} cap={cap}")


def _contract_sort_key(c: KalshiContract) -> int:
    if c.strike_type == "less":
        return -1
    if c.strike_type == "greater":
        return 999
    try:
        return int(c.subtitle.split("°")[0])
    except Exception:
        pass
    # I3: fall back to the numeric strike before the middling 500, so a middle bucket whose
    # subtitle lacks '°' still sorts sensibly. (Cosmetic — every consumer re-sorts by bucket bound.)
    try:
        return int(c.strike) if c.strike is not None else 500
    except (TypeError, ValueError):
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
