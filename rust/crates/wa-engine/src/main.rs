//! Weather-Alpha LIVE engine — Option-A true live-replication daemon.
//!
//! Resident loop: sleep until each city's local anchor (high 17:00 / low 22:00), then fetch the LIVE
//! Kalshi book at that instant, apply the executable per-city rule (`wa-algo`), execute
//! (paper/live), settle prior open positions, persist, and log. One pass + exit with `--once`
//! (defaults to dry-run) for parity checks against the Python paper bot.
//!
//! Flags: `--config <path>` (default `wa.toml`), `--once`, `--market high|low|both`,
//! `--paper` | `--live` | `--dry-run` (override `wa.toml` mode).

mod config;

use anyhow::{Context, Result};
use chrono::{Datelike, Duration as ChronoDuration, SecondsFormat, Utc};
use config::Config;
use serde_json::json;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;
use wa_book::OrderBook;
use wa_exec::Mode;
use wa_kalshi::{KalshiClient, PositionHeld, Signer};
use wa_schedule::{Anchor, City, Market};
use wa_state::{EngineState, OpenPosition};

fn main() -> Result<()> {
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .with_target(false)
        .init();

    let args: Vec<String> = std::env::args().collect();
    let cfg_path = flag_value(&args, "--config").unwrap_or_else(|| "wa.toml".to_string());
    let once = args.iter().any(|a| a == "--once");
    let market_filter = flag_value(&args, "--market").unwrap_or_else(|| "both".into());

    let cfg = Config::load(Path::new(&cfg_path))?;
    let cities = cfg.cities()?;

    // Mode: explicit flag wins; else dry-run for --once (safe), else the config's mode.
    let mode = if args.iter().any(|a| a == "--live") {
        Mode::Live
    } else if args.iter().any(|a| a == "--paper") {
        Mode::Paper
    } else if once || args.iter().any(|a| a == "--dry-run") {
        Mode::DryRun // --once defaults to dry-run (safe)
    } else {
        Mode::parse(&cfg.mode).unwrap_or(Mode::Paper)
    };

    let client = build_client(&cfg, mode)?;

    let mut state = match EngineState::load(&cfg.state_path)? {
        Some(s) => s,
        None => {
            tracing::info!("no state at {} — starting fresh", cfg.state_path.display());
            EngineState::new(cfg.bankroll_init)
        }
    };

    // LIVE sizes off the REAL account, never config (lesson #11): adopt balance × live_alloc_frac as
    // the single shared bankroll both markets size from. Fatal if the balance can't be read — never
    // arm blind on a config number.
    if mode == Mode::Live {
        let bal = client
            .balance_cents()
            .context("LIVE requires a /portfolio/balance read at activation (lesson #11)")?;
        adopt_live_bankroll(&cfg, &mut state, bal);
    }

    tracing::info!(
        mode = mode.as_str(),
        cities = cities.len(),
        once,
        market = %market_filter,
        "wa-engine starting"
    );

    if once {
        let markets = parse_markets(&market_filter);
        eval_once(&cfg, mode, &client, &cities, &mut state, &markets);
    } else {
        run_resident(&cfg, mode, &client, &cities, &mut state)?;
    }
    Ok(())
}

fn build_client(cfg: &Config, mode: Mode) -> Result<KalshiClient> {
    let signer = if mode == Mode::Live {
        let key_id = std::env::var(&cfg.key_id_env)
            .with_context(|| format!("LIVE needs ${}", cfg.key_id_env))?;
        let key_path = std::env::var(&cfg.private_key_path_env)
            .with_context(|| format!("LIVE needs ${}", cfg.private_key_path_env))?;
        Some(Signer::load(key_id, key_path)?)
    } else {
        None
    };
    KalshiClient::new(&cfg.api_base, signer)
}

/// Set the shared LIVE bankroll from the real balance (cents) × `live_alloc_frac`. Logs the new
/// figure on a change and stays quiet otherwise (the per-loop resync calls this every pass).
fn adopt_live_bankroll(cfg: &Config, state: &mut EngineState, balance_cents: i64) {
    let bal = balance_cents as f64 / 100.0;
    let alloc = bal * cfg.live_alloc_frac;
    let changed = state.live_bankroll != Some(alloc);
    state.live_bankroll = Some(alloc);
    if changed {
        tracing::info!(
            balance = format!("{:.2}", bal),
            alloc_pct = format!("{:.0}%", cfg.live_alloc_frac * 100.0),
            live_bankroll = format!("{:.2}", alloc),
            "live bankroll synced to account"
        );
    }
}

/// Re-sync the shared LIVE bankroll to the real account after settlements (the exchange is truth).
/// No-op outside live; on a balance-read failure keep the prior value (never zero a live bankroll).
fn sync_live_bankroll(cfg: &Config, mode: Mode, client: &KalshiClient, state: &mut EngineState) {
    if mode != Mode::Live {
        return;
    }
    match client.balance_cents() {
        Ok(c) => adopt_live_bankroll(cfg, state, c),
        Err(e) => {
            tracing::warn!("live balance resync failed (keeping ${:?}): {e}", state.live_bankroll)
        }
    }
}

/// G4 — adopt any exchange YES-position the Book isn't tracking: an orphan from a crash between place
/// and save (G3), or a fill the confirm-poll missed because `/portfolio/positions` lagged. The
/// exchange is truth, so we self-heal by adopting (reconstructing the open position so it settles)
/// rather than halting. Also clears the pending-order journal. Live-only; a positions-read failure is
/// a no-op (retried next cycle).
fn reconcile_live(cfg: &Config, mode: Mode, client: &KalshiClient, state: &mut EngineState) {
    if mode != Mode::Live {
        return;
    }
    let positions = match client.positions() {
        Ok(p) => p,
        Err(e) => {
            tracing::warn!("reconcile: positions read failed (skipping): {e}");
            return;
        }
    };
    let pending = state.pending.clone();
    let now = now_iso();
    let mut adoptions: Vec<OpenPosition> = Vec::new();
    for p in positions.iter().filter(|p| p.side == "yes" && p.contracts > 0) {
        if state.is_tracked(&p.ticker) {
            continue;
        }
        match open_position_from_exchange(p, &pending, &now) {
            Some(pos) => adoptions.push(pos),
            None => tracing::warn!(ticker = %p.ticker, "reconcile: untracked exchange position with unrecognized ticker — manual review"),
        }
    }
    for pos in adoptions {
        let market = pos.market.clone();
        tracing::warn!(market = %market, ticker = %pos.ticker, contracts = pos.fillable_contracts, vwap = ?pos.fill_vwap, "RECON: adopted orphan exchange position");
        log_event(&cfg.log_path, pos_row(&pos, "RECON"));
        state.book(&market).open.push(pos);
    }
    state.pending = None;
}

/// Reconstruct an `OpenPosition` for an untracked exchange holding. When the pending journal's ticker
/// matches, reuse its rich metadata (city/series/event/anchor) and overwrite the fill with exchange
/// truth; otherwise parse the ticker. Returns None if the ticker maps to no known market.
fn open_position_from_exchange(
    p: &PositionHeld,
    pending: &Option<OpenPosition>,
    now_iso: &str,
) -> Option<OpenPosition> {
    let fill = p.avg_price_cents as f64 / 100.0;
    if let Some(pend) = pending.as_ref().filter(|pd| pd.ticker == p.ticker) {
        let mut pos = pend.clone();
        pos.fillable_contracts = p.contracts as f64; // exchange truth
        pos.fill_vwap = Some(fill);
        pos.settled = false;
        pos.win = None;
        pos.pnl_usd = None;
        return Some(pos);
    }
    let (series, market, event_ticker, event_date) = parse_ticker(&p.ticker)?;
    Some(OpenPosition {
        captured_utc: now_iso.to_string(),
        market,
        city: String::new(),
        series,
        event_ticker,
        ticker: p.ticker.clone(),
        subtitle: String::new(),
        event_date,
        anchor_utc: String::new(),
        mid: fill,
        yes_ask: fill,
        stake_usd: p.contracts as f64 * fill,
        intended_contracts: p.contracts as f64,
        fillable_contracts: p.contracts as f64,
        fill_vwap: Some(fill),
        fillable_pct: 1.0,
        ladder_depth_usd: 0.0,
        mode: "live".to_string(),
        order_id: None,
        settled: false,
        win: None,
        pnl_usd: None,
    })
}

/// `KXHIGHCHI-26JUN16-T80` -> (series, market, event_ticker, event_date). Market from the series
/// prefix (`KXHIGH*` -> high, `KXLOWT*` -> low). None if neither prefix matches or the shape is off.
fn parse_ticker(ticker: &str) -> Option<(String, String, String, String)> {
    let parts: Vec<&str> = ticker.splitn(3, '-').collect();
    if parts.len() < 2 {
        return None;
    }
    let series = parts[0].to_string();
    let market = if series.starts_with("KXHIGH") {
        "high"
    } else if series.starts_with("KXLOWT") {
        "low"
    } else {
        return None;
    }
    .to_string();
    let event_ticker = format!("{}-{}", parts[0], parts[1]);
    let event_date = parse_event_date(parts[1]).unwrap_or_else(|| parts[1].to_string());
    Some((series, market, event_ticker, event_date))
}

/// Kalshi event date-code `26JUN16` -> `2026-06-16` (inverse of `event_date_code`). None on a bad code.
fn parse_event_date(code: &str) -> Option<String> {
    if code.len() != 7 {
        return None;
    }
    const MON: [&str; 12] =
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
    let yy: u32 = code.get(0..2)?.parse().ok()?;
    let mon = code.get(2..5)?;
    let mm = MON.iter().position(|&m| m == mon)? as u32 + 1;
    let dd: u32 = code.get(5..7)?.parse().ok()?;
    Some(format!("20{:02}-{:02}-{:02}", yy, mm, dd))
}

/// Resident scheduler loop: process the soonest due anchor inside its window, sleep otherwise.
fn run_resident(
    cfg: &Config,
    mode: Mode,
    client: &KalshiClient,
    cities: &[City],
    state: &mut EngineState,
) -> Result<()> {
    let window = ChronoDuration::minutes(cfg.anchor_window_min);
    loop {
        let now = Utc::now();
        reconcile_live(cfg, mode, client, state);
        settle_open(cfg, client, state);
        sync_live_bankroll(cfg, mode, client, state);
        state.save(&cfg.state_path)?;

        let upcoming = wa_schedule::upcoming(cities, now - window, 2);
        let next = upcoming.into_iter().find(|a| !state.is_processed(&a.key(), a.event_date));

        match next {
            Some(a) if a.fire_utc <= now => {
                if now < a.fire_utc + window {
                    tracing::info!(anchor = %a.key(), "processing due anchor");
                    process_anchor(&a, state, client, cfg, mode);
                } else {
                    tracing::info!(anchor = %a.key(), "stale anchor past window — skipping (no catch-up)");
                }
                state.mark_processed(a.key(), a.event_date);
                state.save(&cfg.state_path)?;
            }
            Some(a) => {
                let wake = a.fire_utc.min(now + ChronoDuration::seconds(cfg.poll_interval_sec as i64));
                let secs = (wake - now).num_seconds().max(1) as u64;
                tracing::info!(next = %a.key(), fire = %a.fire_utc.to_rfc3339(), "sleeping {secs}s");
                thread::sleep(Duration::from_secs(secs));
            }
            None => thread::sleep(Duration::from_secs(cfg.poll_interval_sec)),
        }
    }
}

/// One-shot evaluation NOW for every city's current event (the dry-run / parity path).
fn eval_once(
    cfg: &Config,
    mode: Mode,
    client: &KalshiClient,
    cities: &[City],
    state: &mut EngineState,
    markets: &[Market],
) {
    let now = Utc::now();
    reconcile_live(cfg, mode, client, state);
    settle_open(cfg, client, state);
    for (i, city) in cities.iter().enumerate() {
        let local_date = now.with_timezone(&city.tz).date_naive();
        for &m in markets {
            let series = match m {
                Market::High => city.series_high.clone(),
                Market::Low => city.series_low.clone(),
            };
            if series.is_empty() {
                continue; // e.g. NYC has no low series
            }
            let a = Anchor {
                city_idx: i,
                city_name: city.name.clone(),
                market: m,
                series,
                event_date: local_date,
                fire_utc: now,
            };
            process_anchor(&a, state, client, cfg, mode);
        }
    }
    // dry-run is informational only — never persist (don't disturb the paper bankroll/state file)
    if mode != Mode::DryRun {
        if let Err(e) = state.save(&cfg.state_path) {
            tracing::error!("state save failed: {e}");
        }
    }
}

/// Decide + (paper/live) execute one city at its anchor. Failures are isolated (logged, return) so
/// one city can never crash the others — matching the Python engine's per-market isolation.
fn process_anchor(
    a: &Anchor,
    state: &mut EngineState,
    client: &KalshiClient,
    cfg: &Config,
    mode: Mode,
) {
    let market = a.market.as_str();
    let date = a.event_date;
    let event_ticker =
        format!("{}-{}", a.series, wa_kalshi::event_date_code(date.year(), date.month(), date.day()));

    // halt gates
    {
        let book = state.book(market);
        if book.account_halted() {
            tracing::warn!(market, city = %a.city_name, "account halted — skip");
            log_event(&cfg.log_path, skip_row(a, &event_ticker, "account_halted", None, 0));
            return;
        }
        if book.city_halted(&a.city_name) {
            tracing::warn!(market, city = %a.city_name, "city halted — skip");
            log_event(&cfg.log_path, skip_row(a, &event_ticker, "city_halted", None, 0));
            return;
        }
    }

    let quotes = match client.event_quotes(&event_ticker) {
        Ok(q) => q,
        Err(e) => {
            tracing::warn!(%event_ticker, "fetch quotes failed: {e}");
            return;
        }
    };

    let live_bankroll = state.live_bankroll; // Copy Option<f64>; read before the &mut book borrow
    let (bankroll, entries) = {
        let book = state.book(market);
        // LIVE: both markets size off ONE shared bankroll (the real account). Paper/dry-run keep the
        // per-market Book bankroll. (Book bankrolls still roll independently for the drawdown halts.)
        let bankroll = match mode {
            Mode::Live => live_bankroll.unwrap_or(book.bankroll_usd),
            _ => book.bankroll_usd,
        };
        (bankroll, book.entries_today(date))
    };
    let acfg = cfg.algo();
    let fav = wa_book::favorite(&quotes).map(|i| (quotes[i].ticker.clone(), quotes[i].mid()));

    // Prelim decision with an empty book — no orderbook fetch on a skip (most cities). Only when it
    // would enter do we pay for the favorite's depth and re-decide with real feasibility.
    match wa_algo::decide(&acfg, &quotes, &OrderBook::default(), bankroll, entries) {
        wa_algo::Decision::Skip { reason, fav_mid } => {
            tracing::info!(market, city = %a.city_name, ?reason, ?fav_mid, "SKIP");
            log_event(
                &cfg.log_path,
                skip_row(a, &event_ticker, &format!("{reason:?}"), fav_mid, quotes.len()),
            );
        }
        wa_algo::Decision::Enter(_) => {
            let fav_ticker = fav.as_ref().map(|(t, _)| t.clone()).unwrap_or_default();
            let fav_book = client.orderbook(&fav_ticker).unwrap_or_default();
            let entry = match wa_algo::decide(&acfg, &quotes, &fav_book, bankroll, entries) {
                wa_algo::Decision::Enter(e) => e,
                _ => return,
            };
            // G5: aggregate exposure cap — bound total open cost beyond the daily entry count.
            // Account-wide in live (one real account); per-market in paper (independent bankrolls).
            let open_exposure = match mode {
                Mode::Live => state.high.open_exposure_usd() + state.low.open_exposure_usd(),
                _ if market == "high" => state.high.open_exposure_usd(),
                _ => state.low.open_exposure_usd(),
            };
            if open_exposure + entry.stake_usd > cfg.total_exposure_max_pct * bankroll {
                tracing::warn!(
                    market,
                    city = %a.city_name,
                    open = format!("{:.2}", open_exposure),
                    add = format!("{:.2}", entry.stake_usd),
                    cap = format!("{:.2}", cfg.total_exposure_max_pct * bankroll),
                    "exposure cap — skip"
                );
                log_event(&cfg.log_path, skip_row(a, &event_ticker, "exposure_cap", Some(entry.mid), quotes.len()));
                return;
            }
            if mode == Mode::Live && wa_exec::kill_switch_active(&cfg.kill_switch_path) {
                tracing::warn!("KILL_SWITCH active — not placing live order for {}", entry.ticker);
                log_event(&cfg.log_path, skip_row(a, &event_ticker, "kill_switch", Some(entry.mid), quotes.len()));
                return;
            }
            let coid = format!("wa-{}-{}", entry.ticker, date);
            // Build the position we intend to open (fill fields are predicted for now; the realized
            // fill overwrites them below for live).
            let mut pos = OpenPosition {
                captured_utc: now_iso(),
                market: market.to_string(),
                city: a.city_name.clone(),
                series: a.series.clone(),
                event_ticker: event_ticker.clone(),
                ticker: entry.ticker.clone(),
                subtitle: entry.subtitle.clone(),
                event_date: date.to_string(),
                anchor_utc: a.fire_utc.to_rfc3339(),
                mid: entry.mid,
                yes_ask: entry.yes_ask,
                stake_usd: entry.stake_usd,
                intended_contracts: entry.want_contracts,
                fillable_contracts: entry.feasibility.fillable_contracts, // predicted; realized below
                fill_vwap: entry.feasibility.fill_vwap,                   // predicted; realized below
                fillable_pct: entry.feasibility.fillable_pct, // depth context at decision time
                ladder_depth_usd: entry.feasibility.ladder_depth_usd,
                mode: mode.as_str().to_string(),
                order_id: None,
                settled: false,
                win: None,
                pnl_usd: None,
            };
            // G3: write-ahead journal the intent BEFORE sending a live order, so a crash between the
            // place and the post-place save is recoverable (G4 adopts it with the true fill). If the
            // journal can't be persisted, don't place — never trade an unrecoverable order.
            if mode == Mode::Live {
                state.pending = Some(pos.clone());
                if let Err(e) = state.save(&cfg.state_path) {
                    tracing::error!(ticker = %entry.ticker, "journal write failed — NOT placing live order: {e}");
                    state.pending = None;
                    return;
                }
            }
            let (order_id, contracts, fill_vwap) = match wa_exec::execute(mode, client, &entry, &coid) {
                Ok(wa_exec::Execution::Filled { order_id, contracts, fill_vwap }) => {
                    (order_id, contracts, fill_vwap)
                }
                Ok(wa_exec::Execution::NoFill) => {
                    // Live order didn't cross (or nothing fillable) — book no position, don't burn the
                    // daily cap on a no-trade. Paper/dry-run never return NoFill.
                    state.pending = None;
                    tracing::warn!(market, city = %a.city_name, ticker = %entry.ticker, "NO FILL — no position recorded");
                    log_event(&cfg.log_path, skip_row(a, &event_ticker, "no_fill", Some(entry.mid), quotes.len()));
                    return;
                }
                Err(e) => {
                    state.pending = None;
                    tracing::error!("execute failed for {}: {e}", entry.ticker);
                    log_event(&cfg.log_path, skip_row(a, &event_ticker, "order_error", Some(entry.mid), quotes.len()));
                    return;
                }
            };
            pos.order_id = order_id;
            pos.fillable_contracts = contracts; // realized fill (live) / predicted achievable (paper)
            pos.fill_vwap = fill_vwap;
            {
                let book = state.book(market);
                book.record_entry(date);
                book.open.push(pos.clone());
            }
            state.pending = None; // intent fulfilled and recorded; clear the journal
            tracing::info!(
                market,
                city = %a.city_name,
                ticker = %pos.ticker,
                conf = format!("{:.3}", pos.mid),
                ask = pos.yes_ask,
                contracts = format!("{:.0}", pos.fillable_contracts),
                fillable = format!("{:.0}%", pos.fillable_pct * 100.0),
                order = ?pos.order_id,
                "ENTER"
            );
            log_event(&cfg.log_path, pos_row(&pos, "ENTER"));
        }
    }
}

/// Settle every open position whose event has settled, across both markets. Books P&L, rolls the
/// per-market bankroll (updating halts), logs a SETTLE row, and drops settled rows from `open`.
fn settle_open(cfg: &Config, client: &KalshiClient, state: &mut EngineState) {
    for market in ["high", "low"] {
        let book = state.book(market);
        if book.open.iter().all(|p| p.settled) {
            continue;
        }
        let mut cache: HashMap<String, Option<String>> = HashMap::new();
        let mut updates: Vec<(usize, bool, f64)> = Vec::new();
        for (i, pos) in book.open.iter().enumerate() {
            if pos.settled {
                continue;
            }
            let winner = cache
                .entry(pos.event_ticker.clone())
                .or_insert_with(|| client.settlement_winner(&pos.event_ticker).ok().flatten())
                .clone();
            if let Some(w) = winner {
                let win = w == pos.ticker;
                updates.push((i, win, wa_exec::settle_pnl(pos, win)));
            }
        }
        if updates.is_empty() {
            continue;
        }
        let mut settled_rows: Vec<OpenPosition> = Vec::new();
        for (i, win, pnl) in updates {
            let city = book.open[i].city.clone();
            book.open[i].settled = true;
            book.open[i].win = Some(win);
            book.open[i].pnl_usd = Some(pnl);
            book.book_settlement(&city, pnl);
            settled_rows.push(book.open[i].clone());
        }
        let bankroll = book.bankroll_usd;
        book.open.retain(|p| !p.settled);
        for r in &settled_rows {
            tracing::info!(market, city = %r.city, ticker = %r.ticker, win = ?r.win, pnl = ?r.pnl_usd, bankroll = format!("{:.2}", bankroll), "SETTLE");
            let mut row = pos_row(r, "SETTLE");
            row["bankroll_after"] = json!(bankroll);
            log_event(&cfg.log_path, row);
        }
    }
}

// ---- small helpers ----------------------------------------------------------------------------

fn flag_value(args: &[String], flag: &str) -> Option<String> {
    args.iter().position(|a| a == flag).and_then(|i| args.get(i + 1)).cloned()
}

fn parse_markets(s: &str) -> Vec<Market> {
    match s {
        "high" => vec![Market::High],
        "low" => vec![Market::Low],
        _ => vec![Market::High, Market::Low],
    }
}

fn now_iso() -> String {
    Utc::now().to_rfc3339_opts(SecondsFormat::Secs, true)
}

fn pos_row(pos: &OpenPosition, event: &str) -> serde_json::Value {
    let mut v = serde_json::to_value(pos).unwrap_or_else(|_| json!({}));
    v["event"] = json!(event);
    v["ts"] = json!(now_iso());
    v
}

fn skip_row(
    a: &Anchor,
    event_ticker: &str,
    reason: &str,
    fav_mid: Option<f64>,
    n_buckets: usize,
) -> serde_json::Value {
    json!({
        "ts": now_iso(),
        "event": "SKIP",
        "market": a.market.as_str(),
        "city": a.city_name,
        "series": a.series,
        "event_ticker": event_ticker,
        "event_date": a.event_date.to_string(),
        "anchor_utc": a.fire_utc.to_rfc3339(),
        "reason": reason,
        "fav_mid": fav_mid,
        "n_buckets": n_buckets,
    })
}

fn log_event(path: &PathBuf, row: serde_json::Value) {
    if let Some(dir) = path.parent() {
        let _ = std::fs::create_dir_all(dir);
    }
    if let Ok(mut f) = std::fs::OpenOptions::new().create(true).append(true).open(path) {
        use std::io::Write;
        let _ = writeln!(f, "{row}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn held(ticker: &str, contracts: i64, cents: i64) -> PositionHeld {
        PositionHeld { ticker: ticker.into(), side: "yes".into(), contracts, avg_price_cents: cents }
    }

    fn pending_pos(ticker: &str) -> OpenPosition {
        OpenPosition {
            captured_utc: "t".into(),
            market: "high".into(),
            city: "Chicago".into(),
            series: "KXHIGHCHI".into(),
            event_ticker: "KXHIGHCHI-26JUN16".into(),
            ticker: ticker.into(),
            subtitle: "80 to 81".into(),
            event_date: "2026-06-16".into(),
            anchor_utc: "a".into(),
            mid: 0.94,
            yes_ask: 0.95,
            stake_usd: 41.0,
            intended_contracts: 43.0,
            fillable_contracts: 43.0, // predicted in the journal; exchange truth must win on adopt
            fill_vwap: Some(0.95),
            fillable_pct: 1.0,
            ladder_depth_usd: 100.0,
            mode: "live".into(),
            order_id: Some("wa-x".into()),
            settled: false,
            win: None,
            pnl_usd: None,
        }
    }

    #[test]
    fn parse_ticker_maps_market_and_date() {
        let (s, m, e, d) = parse_ticker("KXHIGHCHI-26JUN16-T80").unwrap();
        assert_eq!((s.as_str(), m.as_str(), e.as_str(), d.as_str()), ("KXHIGHCHI", "high", "KXHIGHCHI-26JUN16", "2026-06-16"));
        assert_eq!(parse_ticker("KXLOWTCHI-26JUN16-T54").unwrap().1, "low");
        assert!(parse_ticker("FOOBAR-26JUN16-X").is_none()); // unknown series prefix
        assert!(parse_ticker("KXHIGHCHI").is_none()); // no event segment
    }

    #[test]
    fn adopt_orphan_prefers_journal_but_takes_exchange_fill() {
        let p = held("KXHIGHCHI-26JUN16-T80", 40, 95);
        let pos = open_position_from_exchange(&p, &Some(pending_pos("KXHIGHCHI-26JUN16-T80")), "now").unwrap();
        assert_eq!(pos.city, "Chicago"); // metadata enriched from the journal
        assert_eq!(pos.fillable_contracts, 40.0); // ...but the FILL is exchange truth (not journal's 43)
        assert_eq!(pos.fill_vwap, Some(0.95));
        assert!(!pos.settled);
        // a journal for a DIFFERENT ticker is ignored -> reconstruct from the exchange ticker
        let other = open_position_from_exchange(&p, &Some(pending_pos("OTHER-TICKER")), "now").unwrap();
        assert!(other.city.is_empty());
        assert_eq!(other.market, "high");
    }

    #[test]
    fn adopt_orphan_reconstructs_from_ticker_without_journal() {
        let pos = open_position_from_exchange(&held("KXLOWTCHI-26JUN16-T54", 10, 90), &None, "now").unwrap();
        assert_eq!(pos.market, "low");
        assert_eq!(pos.event_ticker, "KXLOWTCHI-26JUN16");
        assert_eq!(pos.fillable_contracts, 10.0);
        assert_eq!(pos.fill_vwap, Some(0.90));
        assert!(pos.city.is_empty()); // sparse metadata without a journal
    }
}
