//! Pure response → domain mapping (no I/O), ported from `kalshi.py` + `directional_paper.py`.
//! Kept separate so it is fully unit-testable without a network.

use serde_json::Value;
use wa_book::{Level, OrderBook, Quote};

/// Parse a Kalshi fixed-point field (a JSON number, or a string like "0.94") to f64. None if absent
/// or unparseable. Mirrors `kalshi._fp` semantics (but returns Option so callers see "missing").
pub fn fp(v: &Value, key: &str) -> Option<f64> {
    let x = v.get(key)?;
    if let Some(f) = x.as_f64() {
        return Some(f);
    }
    x.as_str().and_then(|s| s.parse::<f64>().ok())
}

fn str_field<'a>(m: &'a Value, key: &str) -> Option<&'a str> {
    m.get(key).and_then(|v| v.as_str())
}

/// One `Quote` per market dict from a `/markets` response. `subtitle` falls back to `yes_sub_title`
/// (some series leave `subtitle` null), matching `kalshi._spec_from_market`'s range-text source.
pub fn quotes_from_markets(markets: &[Value]) -> Vec<Quote> {
    markets
        .iter()
        .map(|m| Quote {
            ticker: str_field(m, "ticker").unwrap_or("").to_string(),
            subtitle: str_field(m, "subtitle")
                .filter(|s| !s.is_empty())
                .or_else(|| str_field(m, "yes_sub_title"))
                .unwrap_or("")
                .to_string(),
            yes_bid: fp(m, "yes_bid_dollars"),
            yes_ask: fp(m, "yes_ask_dollars"),
            yes_ask_size: fp(m, "yes_ask_size_fp").or_else(|| fp(m, "yes_ask_size")),
            last: fp(m, "last_price_dollars"),
        })
        .collect()
}

/// Parse a `/markets/{ticker}/orderbook` response into a `wa_book::OrderBook`. Kalshi returns
/// `{"orderbook": {"yes": [[price_cents, size], ...], "no": [[...]]}}` with **integer-cent** prices;
/// convert to dollars so the `wa_book` ladder/feasibility code works in `[0,1]` like the Python.
pub fn orderbook_from_value(ob: &Value) -> OrderBook {
    let inner = ob.get("orderbook").unwrap_or(ob);
    let side = |s: &Value| -> Vec<Level> {
        s.as_array()
            .map(|levels| {
                levels
                    .iter()
                    .filter_map(|lvl| {
                        let arr = lvl.as_array()?;
                        let price_cents = arr.first()?.as_f64()?;
                        let size = arr.get(1)?.as_f64()?;
                        Some(Level { price: price_cents / 100.0, size })
                    })
                    .collect()
            })
            .unwrap_or_default()
    };
    OrderBook {
        yes: inner.get("yes").map(side).unwrap_or_default(),
        no: inner.get("no").map(side).unwrap_or_default(),
    }
}

/// Account balance in integer cents from a `/portfolio/balance` response. Prefers `balance_dollars`
/// (current API) and falls back to the legacy integer-cents `balance`. Port of `_parse_balance_cents`.
pub fn balance_cents(data: &Value) -> i64 {
    if let Some(d) = fp(data, "balance_dollars") {
        return (d * 100.0).round() as i64;
    }
    data.get("balance").and_then(|v| v.as_i64()).unwrap_or(0)
}

/// The single ticker that settled `yes` for an event, else None (not settled / ambiguous).
/// Mirrors `directional_paper.settlement_winner`: prefer `settlement_value`, else `result`.
pub fn settlement_winner(markets: &[Value]) -> Option<String> {
    let winners: Vec<String> = markets
        .iter()
        .filter(|m| {
            let sv = str_field(m, "settlement_value").or_else(|| str_field(m, "result"));
            sv == Some("yes")
        })
        .filter_map(|m| str_field(m, "ticker").map(String::from))
        .collect();
    if winners.len() == 1 {
        Some(winners[0].clone())
    } else {
        None
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn quotes_map_dollar_strings_and_subtitle_fallback() {
        let markets = vec![
            json!({"ticker":"T-A","subtitle":"72° to 73°","yes_bid_dollars":"0.93","yes_ask_dollars":"0.95","last_price_dollars":"0.94"}),
            json!({"ticker":"T-B","subtitle":null,"yes_sub_title":"74° to 75°","yes_ask_dollars":"0.10"}),
        ];
        let qs = quotes_from_markets(&markets);
        assert_eq!(qs[0].ticker, "T-A");
        assert!((qs[0].yes_bid.unwrap() - 0.93).abs() < 1e-12);
        assert!((qs[0].mid() - 0.94).abs() < 1e-12);
        assert_eq!(qs[1].subtitle, "74° to 75°"); // fell back to yes_sub_title
        assert!(qs[1].yes_bid.is_none());
    }

    #[test]
    fn orderbook_converts_cents_to_dollars() {
        let ob = json!({"orderbook":{"yes":[[40,100]],"no":[[6,50],[3,100]]}});
        let book = orderbook_from_value(&ob);
        assert_eq!(book.no.len(), 2);
        assert!((book.no[0].price - 0.06).abs() < 1e-12); // 6¢ -> $0.06
        assert!((book.no[0].size - 50.0).abs() < 1e-12);
        // the wa_book ladder flips these to yes-asks 0.94 / 0.97
        let ladder = wa_book::yes_ask_ladder(&book);
        assert!((ladder[0].0 - 0.94).abs() < 1e-9);
    }

    #[test]
    fn balance_prefers_dollars() {
        assert_eq!(balance_cents(&json!({"balance_dollars":"17.82"})), 1782);
        assert_eq!(balance_cents(&json!({"balance":2356})), 2356);
    }

    #[test]
    fn settlement_winner_needs_exactly_one_yes() {
        let mk = vec![
            json!({"ticker":"W","settlement_value":"yes"}),
            json!({"ticker":"L","settlement_value":"no"}),
        ];
        assert_eq!(settlement_winner(&mk), Some("W".to_string()));
        // none settled yet
        let unsettled = vec![json!({"ticker":"X","result":""})];
        assert_eq!(settlement_winner(&unsettled), None);
    }
}
