//! Order-book domain types + pure decision primitives, ported from
//! `scripts/directional_paper.py` (`_midp`, `yes_ask_ladder`, `feasibility`).
//!
//! Everything here is a pure function of its inputs — no I/O, no clock — so it is trivially
//! unit-tested against the Python reference. Prices are **dollars in `[0, 1]`** throughout
//! (the `wa-kalshi` client converts the API's integer-cent ladders into this form).
//!
//! Complexity: `mid`/`in_band` are O(1); `favorite` is O(buckets); `feasibility` is O(levels).

use serde::{Deserialize, Serialize};

/// One resting order-book level: a price (dollars) and the size resting there (contracts).
#[derive(Clone, Copy, Debug, Default, Serialize, Deserialize)]
pub struct Level {
    pub price: f64,
    pub size: f64,
}

/// A market's two resting ladders. `yes` = resting yes-bids, `no` = resting no-bids.
/// To BUY yes you lift the `no` book (a resting no-bid at `np` is a yes-ask at `1 - np`).
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct OrderBook {
    pub yes: Vec<Level>,
    pub no: Vec<Level>,
}

/// A top-of-book quote for one contract (one Kalshi bucket).
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct Quote {
    pub ticker: String,
    pub subtitle: String,
    pub yes_bid: Option<f64>,
    pub yes_ask: Option<f64>,
    pub yes_ask_size: Option<f64>,
    pub last: Option<f64>,
}

impl Quote {
    /// Confidence proxy = the mid, with the exact fallback chain of `directional_paper._midp`:
    /// `(yes_bid + yes_ask)/2` when both are present, else the first non-zero of `[last, yes_ask]`,
    /// else 0.0. (Python's `or` treats 0.0 as falsy, so a `last` of 0.0 falls through to the ask.)
    pub fn mid(&self) -> f64 {
        match (self.yes_bid, self.yes_ask) {
            (Some(b), Some(a)) => (b + a) / 2.0,
            _ => {
                for x in [self.last, self.yes_ask].into_iter().flatten() {
                    if x != 0.0 {
                        return x;
                    }
                }
                0.0
            }
        }
    }
}

/// Number of "priced" buckets (a yes_ask present) — the engine requires >= 3 to consider an event,
/// matching `directional_paper.capture`'s `len(priced) < 3: continue`.
pub fn n_priced(quotes: &[Quote]) -> usize {
    quotes.iter().filter(|q| q.yes_ask.is_some()).count()
}

/// Index of the favorite = the priced bucket with the highest mid. Ties keep the first seen
/// (matches Python's `max(..., key=...)`). Returns None if no bucket is priced.
pub fn favorite(quotes: &[Quote]) -> Option<usize> {
    let mut best: Option<(usize, f64)> = None;
    for (i, q) in quotes.iter().enumerate() {
        if q.yes_ask.is_none() {
            continue;
        }
        let m = q.mid();
        match best {
            Some((_, bm)) if m <= bm => {}
            _ => best = Some((i, m)),
        }
    }
    best.map(|(i, _)| i)
}

/// Inclusive band test, `lo <= mid <= hi`.
pub fn in_band(mid: f64, lo: f64, hi: f64) -> bool {
    mid >= lo && mid <= hi
}

/// Ascending `(yes_ask_price, size)` ladder to BUY yes, derived from the `no` book
/// (`yp = round(1 - np, 4)`, kept in `(0, 1)`), then sorted. Port of `yes_ask_ladder`.
pub fn yes_ask_ladder(book: &OrderBook) -> Vec<(f64, f64)> {
    let mut out: Vec<(f64, f64)> = Vec::with_capacity(book.no.len());
    for lvl in &book.no {
        let yp = ((1.0 - lvl.price) * 10_000.0).round() / 10_000.0;
        if yp > 0.0 && yp < 1.0 {
            out.push((yp, lvl.size));
        }
    }
    out.sort_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal));
    out
}

/// Result of walking the ask ladder for `want_contracts` up to `cap`. Port of `feasibility`.
#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Feasibility {
    pub best_ask: Option<f64>,
    pub best_ask_size: f64,
    pub fillable_contracts: f64,
    pub fill_vwap: Option<f64>,
    pub fillable_pct: f64,
    pub ladder_depth_usd: f64,
}

/// Walk the ask ladder: contracts fillable at price `<= cap`, their VWAP, best-ask size, and the
/// total resting depth (USD) at or below `cap`. Falls back to a single `(best_ask, best_ask_size)`
/// level when the `no` book is empty (mirrors the Python fallback). O(levels).
pub fn feasibility(
    book: &OrderBook,
    best_ask: Option<f64>,
    best_ask_size: f64,
    want_contracts: f64,
    cap: f64,
) -> Feasibility {
    let mut ladder = yes_ask_ladder(book);
    if ladder.is_empty() {
        if let Some(a) = best_ask {
            ladder = vec![(a, best_ask_size)];
        }
    }
    let mut filled = 0.0_f64;
    let mut cost = 0.0_f64;
    for &(yp, sz) in &ladder {
        if yp > cap {
            break;
        }
        let take = sz.min(want_contracts - filled);
        if take <= 0.0 {
            break;
        }
        filled += take;
        cost += take * yp;
    }
    let vwap = if filled > 0.0 { Some(round4(cost / filled)) } else { None };
    let depth: f64 = ladder.iter().filter(|&&(yp, _)| yp <= cap).map(|&(yp, sz)| yp * sz).sum();
    Feasibility {
        best_ask,
        best_ask_size,
        fillable_contracts: round2(filled),
        fill_vwap: vwap,
        fillable_pct: if want_contracts > 0.0 { round3(filled / want_contracts) } else { 0.0 },
        ladder_depth_usd: round2(depth),
    }
}

fn round2(x: f64) -> f64 {
    (x * 100.0).round() / 100.0
}
fn round3(x: f64) -> f64 {
    (x * 1000.0).round() / 1000.0
}
fn round4(x: f64) -> f64 {
    (x * 10_000.0).round() / 10_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    fn q(ticker: &str, bid: Option<f64>, ask: Option<f64>, last: Option<f64>) -> Quote {
        Quote {
            ticker: ticker.into(),
            subtitle: String::new(),
            yes_bid: bid,
            yes_ask: ask,
            yes_ask_size: None,
            last,
        }
    }

    #[test]
    fn mid_uses_bidask_then_last_then_ask() {
        assert!((q("a", Some(0.92), Some(0.96), None).mid() - 0.94).abs() < 1e-12);
        // no bid -> first non-zero of [last, ask]
        assert!((q("a", None, Some(0.95), Some(0.0)).mid() - 0.95).abs() < 1e-12); // last 0.0 falls through
        assert!((q("a", None, Some(0.95), Some(0.90)).mid() - 0.90).abs() < 1e-12);
        assert_eq!(q("a", None, None, None).mid(), 0.0);
    }

    #[test]
    fn favorite_is_highest_mid_among_priced() {
        let qs = vec![
            q("lo", Some(0.10), Some(0.12), None),
            q("hi", Some(0.93), Some(0.95), None), // mid 0.94 -> favorite
            q("noask", Some(0.99), None, None),    // not priced (no ask)
        ];
        assert_eq!(favorite(&qs), Some(1));
        assert!(in_band(0.94, 0.93, 0.95));
        assert!(!in_band(0.92, 0.93, 0.95));
        assert_eq!(n_priced(&qs), 2);
    }

    #[test]
    fn ladder_flips_no_book_ascending() {
        let book = OrderBook {
            yes: vec![],
            no: vec![
                Level { price: 0.03, size: 100.0 }, // -> yes 0.97
                Level { price: 0.06, size: 50.0 },  // -> yes 0.94
                Level { price: 1.0, size: 9.0 },    // -> yes 0.0 (dropped, not in (0,1))
            ],
        };
        let ladder = yes_ask_ladder(&book);
        assert_eq!(ladder.len(), 2);
        assert!((ladder[0].0 - 0.94).abs() < 1e-9); // ascending: cheapest yes first
        assert!((ladder[1].0 - 0.97).abs() < 1e-9);
    }

    #[test]
    fn feasibility_walks_to_cap_and_vwaps() {
        // want 120 contracts; ladder 50@0.94 then 100@0.97 (cap 0.97).
        let book = OrderBook {
            yes: vec![],
            no: vec![
                Level { price: 0.06, size: 50.0 },  // yes 0.94
                Level { price: 0.03, size: 100.0 }, // yes 0.97
                Level { price: 0.01, size: 80.0 },  // yes 0.99 -> above cap, excluded
            ],
        };
        let f = feasibility(&book, Some(0.94), 50.0, 120.0, 0.97);
        // fills 50@0.94 + 70@0.97 = 120 contracts
        assert!((f.fillable_contracts - 120.0).abs() < 1e-9);
        let want_vwap = (50.0 * 0.94 + 70.0 * 0.97) / 120.0;
        assert!((f.fill_vwap.unwrap() - round4(want_vwap)).abs() < 1e-9);
        assert!((f.fillable_pct - 1.0).abs() < 1e-9);
        // depth at/below cap = 50*0.94 + 100*0.97 (the 0.99 level is excluded)
        assert!((f.ladder_depth_usd - round2(50.0 * 0.94 + 100.0 * 0.97)).abs() < 1e-9);
    }

    #[test]
    fn feasibility_falls_back_to_best_ask_when_no_book_empty() {
        let book = OrderBook::default();
        let f = feasibility(&book, Some(0.95), 8.0, 20.0, 0.97);
        assert!((f.fillable_contracts - 8.0).abs() < 1e-9); // only best-ask size available
        assert!((f.fill_vwap.unwrap() - 0.95).abs() < 1e-9);
        assert!((f.fillable_pct - 0.4).abs() < 1e-9);
    }
}
