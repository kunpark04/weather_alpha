//! Execution routing + settlement P&L + the kill switch.
//!
//! * `DryRun` — decide + log, place nothing (the safe default for `--once`).
//! * `Paper` — record the intended fill (no order); P&L booked at the achievable fill.
//! * `Live` — place a real limit-at-ask taker order for the fillable size (gated by the kill switch
//!   + the Book's halts, which the engine checks first).

use anyhow::Result;
use std::path::Path;
use wa_algo::Entry;
use wa_kalshi::KalshiClient;
use wa_state::OpenPosition;

/// Run mode for the engine.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    DryRun,
    Paper,
    Live,
}

impl Mode {
    pub fn parse(s: &str) -> Option<Mode> {
        match s.to_ascii_lowercase().as_str() {
            "dry" | "dryrun" | "dry-run" => Some(Mode::DryRun),
            "paper" => Some(Mode::Paper),
            "live" => Some(Mode::Live),
            _ => None,
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Mode::DryRun => "dry-run",
            Mode::Paper => "paper",
            Mode::Live => "live",
        }
    }
}

/// True if the kill-switch file exists — when set, no live order is placed.
pub fn kill_switch_active(path: &Path) -> bool {
    path.exists()
}

/// Place a real order iff `mode == Live`. Buys the fillable size at a limit equal to the favorite's
/// best yes-ask (a limit-at-ask taker, per CLAUDE.md lesson #13 — no maker leg). Returns the order id
/// (None for paper/dry-run or when nothing is fillable).
pub fn place_if_live(
    mode: Mode,
    client: &KalshiClient,
    entry: &Entry,
    client_order_id: &str,
) -> Result<Option<String>> {
    if mode != Mode::Live {
        return Ok(None);
    }
    let count = entry.feasibility.fillable_contracts.floor() as i64;
    if count <= 0 {
        tracing::warn!(ticker = %entry.ticker, "live: nothing fillable at/under cap — no order");
        return Ok(None);
    }
    let limit_cents = (entry.yes_ask * 100.0).round() as i64;
    let ack = client.place_order(&entry.ticker, "yes", "buy", count, limit_cents, client_order_id)?;
    let order_id = ack
        .get("order")
        .and_then(|o| o.get("order_id"))
        .or_else(|| ack.get("order_id"))
        .and_then(|v| v.as_str())
        .map(String::from);
    Ok(order_id)
}

/// Settlement P&L for a paper/live pick, booked at the achievable fill (VWAP if we walked the
/// ladder, else the best ask), net of the real Kalshi fee. Port of `directional_paper.settle`'s
/// `cost = c*fill + FEE*c; pnl = (c - cost) if win else -cost`, using the exact `ceil` fee
/// (`wa_fees`) rather than the paper bot's continuous approximation (sub-cent difference).
pub fn settle_pnl(pos: &OpenPosition, win: bool) -> f64 {
    let fill = pos.fill_vwap.filter(|v| *v > 0.0).unwrap_or(pos.yes_ask);
    let contracts = if pos.fillable_contracts > 0.0 {
        pos.fillable_contracts
    } else if fill > 0.0 {
        pos.stake_usd / fill
    } else {
        0.0
    };
    let fee = wa_fees::trade_fee_dollars(fill, contracts.round().max(0.0) as i64);
    let cost = contracts * fill + fee;
    if win {
        contracts - cost
    } else {
        -cost
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pos(fill_vwap: Option<f64>, yes_ask: f64, fillable: f64, stake: f64) -> OpenPosition {
        OpenPosition {
            captured_utc: "x".into(),
            market: "high".into(),
            city: "Chicago".into(),
            series: "KXHIGHCHI".into(),
            event_ticker: "KXHIGHCHI-26JUN15".into(),
            ticker: "KXHIGHCHI-26JUN15-T80".into(),
            subtitle: "".into(),
            event_date: "2026-06-15".into(),
            anchor_utc: "2026-06-15T22:00:00+00:00".into(),
            mid: 0.94,
            yes_ask,
            stake_usd: stake,
            intended_contracts: stake / yes_ask,
            fillable_contracts: fillable,
            fill_vwap,
            fillable_pct: 1.0,
            ladder_depth_usd: 0.0,
            mode: "paper".into(),
            order_id: None,
            settled: false,
            win: None,
            pnl_usd: None,
        }
    }

    #[test]
    fn win_pays_contracts_minus_cost() {
        // 100 contracts @ 0.95; fee = ceil(0.07*100*0.95*0.05*100)=ceil(33.25)=34c=$0.34
        let p = pos(Some(0.95), 0.95, 100.0, 95.0);
        let pnl = settle_pnl(&p, true);
        let expected = 100.0 - (100.0 * 0.95 + 0.34);
        assert!((pnl - expected).abs() < 1e-9, "pnl={pnl} expected={expected}");
    }

    #[test]
    fn loss_is_negative_cost() {
        let p = pos(Some(0.95), 0.95, 100.0, 95.0);
        let pnl = settle_pnl(&p, false);
        let expected = -(100.0 * 0.95 + 0.34);
        assert!((pnl - expected).abs() < 1e-9);
    }

    #[test]
    fn falls_back_to_yes_ask_and_stake_when_no_fill_data() {
        let p = pos(None, 0.94, 0.0, 47.0); // contracts = 47/0.94 = 50
        let pnl = settle_pnl(&p, true);
        let c: f64 = 47.0 / 0.94;
        let fee = wa_fees::trade_fee_dollars(0.94, c.round() as i64);
        assert!((pnl - (c - (c * 0.94 + fee))).abs() < 1e-9);
    }

    #[test]
    fn mode_parse() {
        assert_eq!(Mode::parse("LIVE"), Some(Mode::Live));
        assert_eq!(Mode::parse("dry-run"), Some(Mode::DryRun));
        assert_eq!(Mode::parse("nope"), None);
    }
}
