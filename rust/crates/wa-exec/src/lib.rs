//! Execution routing + settlement P&L + the kill switch.
//!
//! * `DryRun` — decide + log, place nothing (the safe default for `--once`).
//! * `Paper` — record the intended fill (no order); P&L booked at the achievable fill.
//! * `Live` — place a real limit-at-ask taker for the fillable size (the kill switch and the Book's
//!   halts are checked first), then **confirm the actual fill** from `/portfolio/positions` and book
//!   only what truly filled, cancelling any resting remainder.

use anyhow::Result;
use std::path::Path;
use std::time::Duration;
use wa_algo::Entry;
use wa_kalshi::KalshiClient;
use wa_state::OpenPosition;

/// Fill-confirmation poll budget. A marketable limit usually fills within ~1 s, but
/// `/portfolio/positions` can lag the matching engine — poll a few times before concluding the order
/// is resting. Mirrors `execution.py` `_FILL_POLL_TRIES` / `_FILL_POLL_DELAY_S`. LIVE-only.
const FILL_POLL_TRIES: u32 = 3;
const FILL_POLL_DELAY: Duration = Duration::from_millis(700);

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

/// Outcome of executing one decided entry — what (if anything) to record as an open position.
#[derive(Clone, Debug, PartialEq)]
pub enum Execution {
    /// A position to book. For paper/dry-run `contracts`/`fill_vwap` are the *predicted* achievable
    /// fill (the depth walk); for live they are the **confirmed** exchange fill.
    Filled { order_id: Option<String>, contracts: f64, fill_vwap: Option<f64> },
    /// Nothing to book — nothing was fillable, or a live order didn't cross and was cancelled.
    NoFill,
}

/// Route one decided entry to the active mode. Paper/dry-run book the predicted achievable fill (no
/// order). Live places a limit-at-ask taker for the fillable size (a limit-at-ask taker, per CLAUDE.md
/// lesson #13 — no maker leg), then **confirms the actual fill** from `/portfolio/positions` rather
/// than assuming the full size filled at our limit; a resting/partial order books only what truly
/// filled and the remainder is cancelled (port of `execution.py` C1/W3).
pub fn execute(
    mode: Mode,
    client: &KalshiClient,
    entry: &Entry,
    client_order_id: &str,
) -> Result<Execution> {
    match mode {
        Mode::DryRun | Mode::Paper => Ok(Execution::Filled {
            order_id: None,
            contracts: entry.feasibility.fillable_contracts,
            fill_vwap: entry.feasibility.fill_vwap,
        }),
        Mode::Live => place_and_confirm(client, entry, client_order_id),
    }
}

fn place_and_confirm(client: &KalshiClient, entry: &Entry, coid: &str) -> Result<Execution> {
    let count = entry.feasibility.fillable_contracts.floor() as i64;
    if count <= 0 {
        tracing::warn!(ticker = %entry.ticker, "live: nothing fillable at/under cap — no order");
        return Ok(Execution::NoFill);
    }
    // Fat-finger guard: a garbage limit (≤0 or ≥1) must never become a real order.
    if !(entry.limit_price > 0.0 && entry.limit_price < 1.0) {
        tracing::error!(ticker = %entry.ticker, limit = entry.limit_price, "live: limit_price outside (0,1) — refusing order");
        return Ok(Execution::NoFill);
    }
    // Limit = the feasibility cap (price we sized fills up to), not the best ask: fills the sized
    // quantity cheapest-first across the ladder and stays marketable through small upticks.
    let limit_cents = (entry.limit_price * 100.0).round() as i64;
    // Pre-order holding (the engine is normally flat on a fresh event-date ticker, but confirm via
    // before/after deltas like the Python path so topping up an existing holding can't double-count).
    let (before_qty, before_avg, _) = held(client, &entry.ticker);
    let ack = client.place_order(&entry.ticker, "yes", "buy", count, limit_cents, coid)?;
    let order_id = ack
        .get("order")
        .and_then(|o| o.get("order_id"))
        .or_else(|| ack.get("order_id"))
        .and_then(|v| v.as_str())
        .map(String::from);

    // Confirm the ACTUAL fill from the exchange (positions can lag the match → poll).
    let (mut filled, mut price_cents, mut confirmed) = (0i64, limit_cents, false);
    for attempt in 0..FILL_POLL_TRIES {
        let (after_qty, after_avg, read_ok) = held(client, &entry.ticker);
        if read_ok {
            confirmed = true;
            filled = (after_qty - before_qty).max(0);
            if filled > 0 {
                // Marginal price of THIS fill (back out the pre-order holding), not the blended avg.
                let marginal = (after_qty * after_avg - before_qty * before_avg) as f64 / filled as f64;
                price_cents = if marginal > 0.0 { marginal.round() as i64 } else { limit_cents };
                break;
            }
        }
        if attempt + 1 < FILL_POLL_TRIES {
            std::thread::sleep(FILL_POLL_DELAY);
        }
    }

    if !confirmed {
        // Positions unreadable on every attempt — we cannot tell if it filled. Cancel best-effort and
        // book NOTHING (never a phantom); if it did fill it's an orphan until reconciliation (G4), so
        // surface it loudly.
        cancel_quietly(client, &order_id);
        tracing::error!(ticker = %entry.ticker, order = ?order_id, "LIVE: fill unconfirmable (positions unreadable) — cancelled best-effort, booked nothing; RECONCILE MANUALLY");
        return Ok(Execution::NoFill);
    }
    if filled <= 0 {
        cancel_quietly(client, &order_id); // retract the resting order so it can't fill after we move on
        tracing::warn!(ticker = %entry.ticker, order = ?order_id, tries = FILL_POLL_TRIES, "LIVE: no fill — cancelled resting order");
        return Ok(Execution::NoFill);
    }
    if filled < count {
        cancel_quietly(client, &order_id); // partial — retract the remainder so it can't fill after booking
        tracing::warn!(ticker = %entry.ticker, filled, requested = count, "LIVE: partial fill — cancelled remainder");
    }
    Ok(Execution::Filled { order_id, contracts: filled as f64, fill_vwap: Some(price_cents as f64 / 100.0) })
}

/// `(contracts, avg_price_cents, read_ok)` held for `ticker` on the YES side. `read_ok` is false when
/// the positions call errored, so the caller keeps polling instead of misreading a transient flat.
fn held(client: &KalshiClient, ticker: &str) -> (i64, i64, bool) {
    match client.positions() {
        Ok(ps) => ps
            .iter()
            .find(|p| p.ticker == ticker && p.side == "yes")
            .map(|p| (p.contracts, p.avg_price_cents, true))
            .unwrap_or((0, 0, true)),
        Err(e) => {
            tracing::warn!(ticker, "positions read failed during fill confirm: {e}");
            (0, 0, false)
        }
    }
}

fn cancel_quietly(client: &KalshiClient, order_id: &Option<String>) {
    if let Some(id) = order_id {
        if let Err(e) = client.cancel_order(id) {
            tracing::warn!(order = %id, "cancel_order failed (best-effort): {e}");
        }
    }
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

    #[test]
    fn paper_and_dryrun_book_predicted_fill_without_network() {
        // A keyless client: any signed call would error, so passing this proves paper/dry-run never
        // hit the network — they book the predicted (achievable) fill straight from the feasibility.
        let client = KalshiClient::new(wa_kalshi::DEFAULT_API_BASE, None).unwrap();
        let entry = Entry {
            ticker: "T-FAV".into(),
            subtitle: String::new(),
            mid: 0.94,
            yes_ask: 0.95,
            limit_price: 0.97,
            stake_usd: 41.67,
            want_contracts: 43.86,
            feasibility: wa_book::Feasibility {
                fillable_contracts: 40.0,
                fill_vwap: Some(0.95),
                ..Default::default()
            },
        };
        let want = Execution::Filled { order_id: None, contracts: 40.0, fill_vwap: Some(0.95) };
        assert_eq!(execute(Mode::Paper, &client, &entry, "wa-T-FAV-2026-06-16").unwrap(), want);
        assert_eq!(execute(Mode::DryRun, &client, &entry, "wa-T-FAV-2026-06-16").unwrap(), want);
    }
}
