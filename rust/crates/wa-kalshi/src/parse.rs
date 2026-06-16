//! Pure response → domain mapping (no I/O), ported from `kalshi.py` + `directional_paper.py`.
//! Kept separate so it is fully unit-testable without a network.

use serde_json::Value;
use wa_book::{Level, OrderBook, Quote};

/// Coerce a single JSON scalar that may be a number OR a numeric string (e.g. "0.0100") to f64.
/// None if it is neither. Shared by `fp` (keyed lookup) and the orderbook ladder parser, both of
/// which face Kalshi's mix of numeric and stringified fixed-point fields.
fn as_num(x: &Value) -> Option<f64> {
    x.as_f64().or_else(|| x.as_str().and_then(|s| s.parse::<f64>().ok()))
}

/// Parse a Kalshi fixed-point field (a JSON number, or a string like "0.94") to f64. None if absent
/// or unparseable. Mirrors `kalshi._fp` semantics (but returns Option so callers see "missing").
pub fn fp(v: &Value, key: &str) -> Option<f64> {
    as_num(v.get(key)?)
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

/// Parse a `/markets/{ticker}/orderbook` response into a `wa_book::OrderBook`. The live Kalshi public
/// endpoint serves the book under **`"orderbook_fp"`** with **already-dollar** `"yes_dollars"`/
/// `"no_dollars"` ladders whose prices *and* sizes are JSON strings (e.g. `["0.0100","2952.00"]` =
/// $0.01 × 2952). The legacy shape was **`"orderbook"`** with `"yes"`/`"no"` **integer-cent** ladders.
/// Read `orderbook_fp` as-is (no scaling) and fall back to the legacy cents shape (÷100) so both work
/// and the `wa_book` ladder/feasibility code always sees dollars in `[0,1]`. Mirrors
/// `directional_paper.fetch_orderbook` / `orderbook_logger.build_record`.
pub fn orderbook_from_value(ob: &Value) -> OrderBook {
    // [[price, size], ...] -> Vec<Level>, multiplying the price by `scale` (1.0 for dollar ladders,
    // 0.01 for legacy cents). Both fields may be numbers or numeric strings, so coerce via `as_num`.
    let side = |s: &Value, scale: f64| -> Vec<Level> {
        s.as_array()
            .map(|levels| {
                levels
                    .iter()
                    .filter_map(|lvl| {
                        let arr = lvl.as_array()?;
                        let price = as_num(arr.first()?)?;
                        let size = as_num(arr.get(1)?)?;
                        Some(Level { price: price * scale, size })
                    })
                    .collect()
            })
            .unwrap_or_default()
    };
    // Current API: dollar-denominated `orderbook_fp` (no scaling).
    if let Some(fpb) = ob.get("orderbook_fp") {
        return OrderBook {
            yes: fpb.get("yes_dollars").map(|s| side(s, 1.0)).unwrap_or_default(),
            no: fpb.get("no_dollars").map(|s| side(s, 1.0)).unwrap_or_default(),
        };
    }
    // Legacy fallback: integer-cent `orderbook` (or a bare `{yes,no}` object) -> dollars.
    let inner = ob.get("orderbook").unwrap_or(ob);
    OrderBook {
        yes: inner.get("yes").map(|s| side(s, 0.01)).unwrap_or_default(),
        no: inner.get("no").map(|s| side(s, 0.01)).unwrap_or_default(),
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
    fn orderbook_reads_orderbook_fp_dollar_strings() {
        // The live public endpoint serves the book under `orderbook_fp` with already-dollar
        // `*_dollars` ladders whose price AND size are strings (e.g. ["0.0100","2952.00"]). They
        // must be read as-is — NO cents conversion — or armed sizing runs against an empty book.
        let ob = json!({"orderbook_fp":{
            "yes_dollars":[["0.4000","100.00"]],
            "no_dollars":[["0.0600","50.00"],["0.0300","100.00"]],
        }});
        let book = orderbook_from_value(&ob);
        assert_eq!(book.yes.len(), 1);
        assert_eq!(book.no.len(), 2);
        assert!((book.yes[0].price - 0.40).abs() < 1e-12); // "0.4000" stays $0.40 (no /100)
        assert!((book.no[0].price - 0.06).abs() < 1e-12); // "0.0600" stays $0.06
        assert!((book.no[0].size - 50.0).abs() < 1e-12); // "50.00" -> 50 contracts
        // the wa_book ladder flips the no-book to ascending yes-asks 0.94 / 0.97
        let ladder = wa_book::yes_ask_ladder(&book);
        assert!((ladder[0].0 - 0.94).abs() < 1e-9);
        assert!((ladder[1].0 - 0.97).abs() < 1e-9);
    }

    #[test]
    fn orderbook_legacy_cents_fallback() {
        // Older shape: `orderbook` with integer-cent ladders -> scaled cents->dollars.
        let ob = json!({"orderbook":{"yes":[[40,100]],"no":[[6,50],[3,100]]}});
        let book = orderbook_from_value(&ob);
        assert_eq!(book.no.len(), 2);
        assert!((book.no[0].price - 0.06).abs() < 1e-12); // 6¢ -> $0.06
        assert!((book.no[0].size - 50.0).abs() < 1e-12);
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
