//! The executable **Option-A** decision rule — a pure function of one city's anchor snapshot.
//!
//! This is the live-faithful rule, and it differs deliberately from the Python paper scorer
//! (`directional_paper.capture`). The scorer is a *batch* scorer: it gathers every city's favorite
//! for the day, ranks them, takes the top 3, and splits 50% of the bankroll across however many it
//! picked (`stake_each = 0.50 / len(picked)`). That dynamic split needs to know *all* cities — i.e.
//! it is cross-city look-ahead, not executable live.
//!
//! At a city's own anchor a live bot knows only the cities that have *already* fired (anchors fire
//! east -> west through the day). So the executable rule reserves the 50% as `daily_cap` equal
//! slots of `stake_fraction / daily_cap` each; each in-band city takes one slot as its anchor fires,
//! first-come, until the cap is hit. On a 3-in-band night this matches the scorer exactly; on a
//! 1-2 city night it deploys less than 50% — the honest cost of having no look-ahead.
//!
//! Pure and O(buckets) (dominated by `favorite`); no I/O, no clock.

use serde::{Deserialize, Serialize};
use wa_book::{favorite, feasibility, in_band, n_priced, Feasibility, OrderBook, Quote};

/// Tunables for the rule (defaults match report §6f / `directional_paper.py`).
#[derive(Clone, Copy, Debug, Serialize, Deserialize)]
pub struct AlgoCfg {
    /// Inclusive favorite-mid band, e.g. (0.93, 0.95).
    pub band_lo: f64,
    pub band_hi: f64,
    /// Max entries per day across all cities (the "top-3" cap).
    pub daily_cap: u32,
    /// Total fraction of bankroll reserved for the day (split into `daily_cap` equal slots).
    pub stake_fraction: f64,
    /// Walk the ask ladder only up to this price when checking feasibility.
    pub cap_price: f64,
}

impl Default for AlgoCfg {
    fn default() -> Self {
        Self { band_lo: 0.93, band_hi: 0.95, daily_cap: 3, stake_fraction: 0.50, cap_price: 0.97 }
    }
}

impl AlgoCfg {
    /// Dollars staked on a single entry = one of `daily_cap` equal slots of `stake_fraction`.
    pub fn per_entry_usd(&self, bankroll: f64) -> f64 {
        if self.daily_cap == 0 {
            return 0.0;
        }
        (self.stake_fraction / self.daily_cap as f64) * bankroll
    }
}

/// Why a city was not entered.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum SkipReason {
    /// Fewer than 3 priced buckets — not a real, tradeable event yet.
    TooFewBuckets,
    /// No bucket had a yes_ask (no favorite).
    NoFavorite,
    /// Favorite mid outside the band.
    OutOfBand,
    /// Already took `daily_cap` entries today.
    DailyCapReached,
}

/// An entry to place (paper or live).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct Entry {
    pub ticker: String,
    pub subtitle: String,
    pub mid: f64,
    pub yes_ask: f64,
    /// Taker limit price for the order = `cfg.cap_price` — the price up to which `feasibility` counted
    /// fillable depth. Placing at this (not the best ask) fills the sized quantity cheapest-first
    /// across the ladder and stays marketable through small upticks; the realized VWAP (≤ this) is
    /// confirmed from the exchange post-fill. Keeps live execution consistent with the cap-walk sizing
    /// (and the paper shadow, which books at the cap-walk VWAP).
    pub limit_price: f64,
    pub stake_usd: f64,
    pub want_contracts: f64,
    pub feasibility: Feasibility,
}

/// The decision for one city at its anchor.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub enum Decision {
    Enter(Entry),
    Skip { reason: SkipReason, fav_mid: Option<f64> },
}

impl Decision {
    pub fn is_enter(&self) -> bool {
        matches!(self, Decision::Enter(_))
    }
}

/// Decide one city at its anchor from its bucket quotes + the favorite's order book.
///
/// * `quotes` — every bucket's top-of-book for this event at the anchor.
/// * `fav_book` — the favorite contract's depth (for the feasibility walk). Pass an empty book to
///   fall back to the favorite's best-ask size only.
/// * `bankroll` — the current paper/live bankroll (constant within a day).
/// * `entries_today` — entries already taken today across all cities (the cap counter).
pub fn decide(
    cfg: &AlgoCfg,
    quotes: &[Quote],
    fav_book: &OrderBook,
    bankroll: f64,
    entries_today: u32,
) -> Decision {
    if n_priced(quotes) < 3 {
        return Decision::Skip { reason: SkipReason::TooFewBuckets, fav_mid: None };
    }
    let Some(fi) = favorite(quotes) else {
        return Decision::Skip { reason: SkipReason::NoFavorite, fav_mid: None };
    };
    let fav = &quotes[fi];
    let mid = fav.mid();
    if !in_band(mid, cfg.band_lo, cfg.band_hi) {
        return Decision::Skip { reason: SkipReason::OutOfBand, fav_mid: Some(mid) };
    }
    if entries_today >= cfg.daily_cap {
        return Decision::Skip { reason: SkipReason::DailyCapReached, fav_mid: Some(mid) };
    }
    // Priced ⇒ yes_ask is Some; size the entry against it.
    let yes_ask = fav.yes_ask.expect("priced favorite has a yes_ask");
    let stake_usd = cfg.per_entry_usd(bankroll);
    let want_contracts = if yes_ask > 0.0 { stake_usd / yes_ask } else { 0.0 };
    let feas = feasibility(
        fav_book,
        Some(yes_ask),
        fav.yes_ask_size.unwrap_or(0.0),
        want_contracts,
        cfg.cap_price,
    );
    Decision::Enter(Entry {
        ticker: fav.ticker.clone(),
        subtitle: fav.subtitle.clone(),
        mid,
        yes_ask,
        limit_price: cfg.cap_price,
        stake_usd,
        want_contracts,
        feasibility: feas,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use wa_book::Level;

    fn quote(ticker: &str, bid: f64, ask: f64) -> Quote {
        Quote {
            ticker: ticker.into(),
            subtitle: String::new(),
            yes_bid: Some(bid),
            yes_ask: Some(ask),
            yes_ask_size: Some(500.0),
            last: None,
        }
    }

    fn event_with_favorite(fav_mid_bid: f64, fav_mid_ask: f64) -> Vec<Quote> {
        vec![
            quote("lo1", 0.01, 0.03),
            quote("lo2", 0.02, 0.04),
            quote("fav", fav_mid_bid, fav_mid_ask),
        ]
    }

    #[test]
    fn enters_in_band_under_cap_with_one_slot_sizing() {
        let cfg = AlgoCfg::default();
        let qs = event_with_favorite(0.93, 0.95); // mid 0.94, in band
        let book = OrderBook { yes: vec![], no: vec![Level { price: 0.04, size: 1000.0 }] }; // yes 0.96
        let d = decide(&cfg, &qs, &book, 250.0, 0);
        match d {
            Decision::Enter(e) => {
                assert_eq!(e.ticker, "fav");
                // one of 3 slots of 50% => 16.667% of 250 = 41.667
                assert!((e.stake_usd - (0.50 / 3.0) * 250.0).abs() < 1e-9);
                assert!((e.want_contracts - e.stake_usd / 0.95).abs() < 1e-9);
                assert!((e.limit_price - cfg.cap_price).abs() < 1e-12); // limit = cap, not best ask
            }
            _ => panic!("expected Enter, got {d:?}"),
        }
    }

    #[test]
    fn skips_out_of_band() {
        let cfg = AlgoCfg::default();
        let qs = event_with_favorite(0.90, 0.92); // mid 0.91 < 0.93
        let d = decide(&cfg, &qs, &OrderBook::default(), 250.0, 0);
        assert_eq!(d, Decision::Skip { reason: SkipReason::OutOfBand, fav_mid: Some(0.91) });
    }

    #[test]
    fn skips_when_daily_cap_reached_even_if_in_band() {
        let cfg = AlgoCfg::default();
        let qs = event_with_favorite(0.93, 0.95);
        let d = decide(&cfg, &qs, &OrderBook::default(), 250.0, 3);
        assert_eq!(d, Decision::Skip { reason: SkipReason::DailyCapReached, fav_mid: Some(0.94) });
    }

    #[test]
    fn skips_too_few_buckets() {
        let cfg = AlgoCfg::default();
        let qs = vec![quote("a", 0.93, 0.95), quote("b", 0.10, 0.12)]; // only 2 priced
        let d = decide(&cfg, &qs, &OrderBook::default(), 250.0, 0);
        assert_eq!(d, Decision::Skip { reason: SkipReason::TooFewBuckets, fav_mid: None });
    }
}
