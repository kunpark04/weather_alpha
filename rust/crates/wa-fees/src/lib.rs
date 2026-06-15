//! Kalshi fee model — a direct port of `weather_alpha/fees.py`.
//!
//! Kalshi's per-side fee: `fee_cents = ceil(7% * N * P * (1 - P) * 100)` for `N` contracts at
//! price `P` dollars. It peaks near `P = 0.5` (~$1.75 / 100 contracts) and shrinks toward the
//! edges. The ceil-to-cent is applied once on the whole leg (per trade), not per contract.
//!
//! All functions are O(1) and allocation-free.

/// Default fee rate (7%).
pub const FEE_RATE: f64 = 0.07;

/// Per-trade fee in **cents**. `price_dollars` is clamped to `[0, 1]`; non-positive `contracts`
/// pays nothing. Matches `fees.trade_fee_cents`.
pub fn trade_fee_cents(price_dollars: f64, contracts: i64, fee_rate: f64) -> i64 {
    if contracts <= 0 {
        return 0;
    }
    let p = price_dollars.clamp(0.0, 1.0);
    let raw = fee_rate * contracts as f64 * p * (1.0 - p) * 100.0; // 100x -> cents
    raw.ceil() as i64
}

/// Per-trade fee in **dollars** at the default 7% rate — the convenience used by the engine.
pub fn trade_fee_dollars(price_dollars: f64, contracts: i64) -> f64 {
    trade_fee_cents(price_dollars, contracts, FEE_RATE) as f64 / 100.0
}

/// Minimum gross edge per contract (cents) needed to clear fees. Matches `fees.break_even_edge_cents`.
pub fn break_even_edge_cents(price_dollars: f64, contracts: i64, fee_rate: f64) -> f64 {
    if contracts <= 0 {
        return 0.0;
    }
    trade_fee_cents(price_dollars, contracts, fee_rate) as f64 / contracts as f64
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn peak_at_half_matches_python_reference() {
        // Mathematically 7*100*0.25 = 175, but `0.07*100*0.5*0.5*100` is 175.00000000000003 in
        // IEEE-754, so ceil -> 176. The Python reference (fees.trade_fee_cents) produces the SAME
        // 176 via the identical float path, so 176 is the parity-correct value (verified, not a fudge).
        assert_eq!(trade_fee_cents(0.5, 100, FEE_RATE), 176);
    }

    #[test]
    fn single_contract_near_edge_rounds_up_to_one_cent() {
        // ceil(0.07 * 1 * 0.94 * 0.06 * 100) = ceil(0.3948) = 1
        assert_eq!(trade_fee_cents(0.94, 1, FEE_RATE), 1);
        assert!((trade_fee_dollars(0.94, 1) - 0.01).abs() < 1e-9);
    }

    #[test]
    fn nonpositive_or_clamped() {
        assert_eq!(trade_fee_cents(0.5, 0, FEE_RATE), 0);
        assert_eq!(trade_fee_cents(0.5, -5, FEE_RATE), 0);
        // p clamped: P=2.0 -> 1.0 -> p*(1-p)=0 -> 0 cents
        assert_eq!(trade_fee_cents(2.0, 100, FEE_RATE), 0);
    }

    #[test]
    fn break_even_matches_per_contract() {
        // 176 cents over 100 contracts = 1.76 cents/contract (matches Python: break_even = 1.76)
        assert!((break_even_edge_cents(0.5, 100, FEE_RATE) - 1.76).abs() < 1e-9);
    }
}
