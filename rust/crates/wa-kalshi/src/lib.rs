//! Kalshi REST client (blocking + rustls) — port of `weather_alpha/kalshi.py`.
//!
//! Market data (`/markets`, `/markets/{t}/orderbook`, settlement) is **public** (keyless), matching
//! the Python paper path; only portfolio + orders are **signed**. Construct with `signer: None` for a
//! pure public/paper client, or `Some(Signer)` to enable the authenticated calls.
//!
//! Uses `reqwest::blocking` (no async runtime needed for a once-per-anchor flow) with `rustls-tls`
//! so the musl static binary carries no OpenSSL C dependency. Retries mirror `kalshi.py`: public
//! GETs ride out 429/5xx with Retry-After-aware backoff; signed calls retry 5xx with backoff.

mod auth;
mod parse;

pub use auth::Signer;
pub use wa_book::{Level, OrderBook, Quote};

use anyhow::{anyhow, Result};
use serde_json::Value;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const DEFAULT_API_BASE: &str = "https://api.elections.kalshi.com/trade-api/v2";

const READ_RETRIES: u32 = 6;
const SIGNED_RETRIES: u32 = 3;
const BACKOFF_CAP: Duration = Duration::from_secs(3);

pub struct KalshiClient {
    http: reqwest::blocking::Client,
    api_base: String,
    base_path: String, // e.g. "/trade-api/v2" — the prefix that must be in the signed path
    signer: Option<Signer>,
}

impl KalshiClient {
    pub fn new(api_base: impl Into<String>, signer: Option<Signer>) -> Result<Self> {
        let api_base = api_base.into();
        let base_path = reqwest::Url::parse(&api_base)?.path().trim_end_matches('/').to_string();
        let http = reqwest::blocking::Client::builder()
            .user_agent("weather-alpha-rs/0.1")
            .timeout(Duration::from_secs(25))
            .build()?;
        Ok(Self { http, api_base, base_path, signer })
    }

    pub fn is_authenticated(&self) -> bool {
        self.signer.is_some()
    }

    fn url(&self, path: &str) -> String {
        format!("{}{}", self.api_base, path)
    }

    // ---- public (keyless) endpoints ------------------------------------------------------------

    fn get_public(&self, path: &str, query: &[(&str, &str)]) -> Result<Value> {
        let url = self.url(path);
        let mut delay = Duration::from_millis(500);
        for attempt in 0..READ_RETRIES {
            let r = match self.http.get(&url).query(query).send() {
                Ok(r) => r,
                Err(e) => {
                    if attempt + 1 >= READ_RETRIES {
                        return Err(e.into());
                    }
                    std::thread::sleep(delay);
                    delay = (delay * 2).min(BACKOFF_CAP);
                    continue;
                }
            };
            let code = r.status().as_u16();
            if code != 429 && code < 500 {
                let r = r.error_for_status()?; // 2xx -> ok; other 4xx -> real error, raise
                return Ok(r.json()?);
            }
            if attempt + 1 >= READ_RETRIES {
                r.error_for_status()?; // exhausted -> surface the 429/5xx
                return Err(anyhow!("GET {path}: retries exhausted"));
            }
            let wait = r
                .headers()
                .get(reqwest::header::RETRY_AFTER)
                .and_then(|v| v.to_str().ok())
                .and_then(|s| s.parse::<f64>().ok())
                .map(Duration::from_secs_f64)
                .unwrap_or(delay)
                .min(BACKOFF_CAP);
            tracing::debug!(path, code, attempt, ?wait, "public GET retry");
            std::thread::sleep(wait);
            delay = (delay * 2).min(BACKOFF_CAP);
        }
        unreachable!("loop returns within READ_RETRIES")
    }

    /// Top-of-book quotes for one event's buckets (`/markets?event_ticker=`).
    pub fn event_quotes(&self, event_ticker: &str) -> Result<Vec<Quote>> {
        let data = self.get_public("/markets", &[("event_ticker", event_ticker)])?;
        let markets = data.get("markets").and_then(|v| v.as_array()).cloned().unwrap_or_default();
        Ok(parse::quotes_from_markets(&markets))
    }

    /// Full depth for one contract (`/markets/{ticker}/orderbook`).
    pub fn orderbook(&self, ticker: &str) -> Result<OrderBook> {
        let data = self.get_public(&format!("/markets/{ticker}/orderbook"), &[])?;
        Ok(parse::orderbook_from_value(&data))
    }

    /// The ticker that settled `yes` for an event, else None (not settled yet / ambiguous).
    pub fn settlement_winner(&self, event_ticker: &str) -> Result<Option<String>> {
        let data =
            self.get_public("/markets", &[("event_ticker", event_ticker), ("limit", "100")])?;
        let markets = data.get("markets").and_then(|v| v.as_array()).cloned().unwrap_or_default();
        Ok(parse::settlement_winner(&markets))
    }

    // ---- signed (authenticated) endpoints ------------------------------------------------------

    fn signed(&self, method: reqwest::Method, path: &str, body: Option<Value>) -> Result<Value> {
        let signer = self.signer.as_ref().ok_or_else(|| anyhow!("client is not authenticated"))?;
        let url = self.url(path);
        let signed_path = format!("{}{}", self.base_path, path);
        let mut delay = Duration::from_millis(500);
        for attempt in 0..SIGNED_RETRIES {
            let ts = now_ms();
            let sig = signer.sign(ts, method.as_str(), &signed_path);
            let mut req = self
                .http
                .request(method.clone(), &url)
                .header("KALSHI-ACCESS-KEY", &signer.key_id)
                .header("KALSHI-ACCESS-SIGNATURE", sig)
                .header("KALSHI-ACCESS-TIMESTAMP", ts.to_string())
                .header(reqwest::header::ACCEPT, "application/json");
            if let Some(b) = &body {
                req = req.json(b);
            }
            let r = match req.send() {
                Ok(r) => r,
                Err(e) => {
                    if attempt + 1 >= SIGNED_RETRIES {
                        return Err(e.into());
                    }
                    std::thread::sleep(delay);
                    delay *= 2;
                    continue;
                }
            };
            if r.status().is_server_error() && attempt + 1 < SIGNED_RETRIES {
                std::thread::sleep(delay);
                delay *= 2;
                continue;
            }
            let r = r.error_for_status()?;
            let text = r.text()?;
            return Ok(if text.is_empty() { Value::Null } else { serde_json::from_str(&text)? });
        }
        Err(anyhow!("signed {path}: retries exhausted"))
    }

    /// Account balance in cents (`/portfolio/balance`). The LIVE bankroll source of truth.
    pub fn balance_cents(&self) -> Result<i64> {
        let data = self.signed(reqwest::Method::GET, "/portfolio/balance", None)?;
        Ok(parse::balance_cents(&data))
    }

    /// Raw held positions (`/portfolio/positions`) for reconciliation. Returned as the raw JSON to
    /// avoid over-modeling a schema the core loop doesn't need.
    pub fn positions_raw(&self) -> Result<Value> {
        self.signed(reqwest::Method::GET, "/portfolio/positions", None)
    }

    /// Place a limit order (`POST /portfolio/orders`). LIVE-only; mirrors `kalshi.place_order`.
    pub fn place_order(
        &self,
        ticker: &str,
        side: &str,   // "yes" | "no"
        action: &str, // "buy" | "sell"
        count: i64,
        limit_price_cents: i64,
        client_order_id: &str,
    ) -> Result<Value> {
        if !(side == "yes" || side == "no") || !(action == "buy" || action == "sell") {
            return Err(anyhow!("bad side/action: {side}/{action}"));
        }
        if count <= 0 {
            return Err(anyhow!("count must be positive: {count}"));
        }
        let mut payload = serde_json::json!({
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
            "client_order_id": client_order_id,
        });
        if side == "yes" {
            payload["yes_price"] = serde_json::json!(limit_price_cents);
        } else {
            payload["no_price"] = serde_json::json!(limit_price_cents);
        }
        tracing::info!(ticker, side, action, count, limit_price_cents, "place_order");
        self.signed(reqwest::Method::POST, "/portfolio/orders", Some(payload))
    }
}

fn now_ms() -> i64 {
    SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_millis() as i64).unwrap_or(0)
}

/// Kalshi event-ticker date code, e.g. `26JUN15` — locale-independent (port of `event_date_code`).
pub fn event_date_code(year: i32, month: u32, day: u32) -> String {
    const MON: [&str; 12] =
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
    format!("{:02}{}{:02}", year % 100, MON[(month as usize - 1).min(11)], day)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn date_code_matches_python() {
        assert_eq!(event_date_code(2026, 6, 15), "26JUN15");
        assert_eq!(event_date_code(2026, 1, 1), "26JAN01");
        assert_eq!(event_date_code(2026, 12, 31), "26DEC31");
    }

    #[test]
    fn base_path_is_extracted_for_signing() {
        let c = KalshiClient::new(DEFAULT_API_BASE, None).unwrap();
        assert_eq!(c.base_path, "/trade-api/v2");
        assert!(!c.is_authenticated());
    }
}
