//! Persistent strategy state — one `Book` **per market** (the Python paper bot keeps independent
//! high/low bankrolls, so we mirror that). A `Book` owns its bankroll, a per-event-date entry
//! counter (the daily cap-3), latched drawdown halts, and the open positions awaiting settlement.
//!
//! Persistence is a single JSON file written atomically (temp + rename). The risk halts are a
//! conservative safety gate (account 50% / per-city 25% drawdown, latched until manual reset) in the
//! spirit of the old `positions.Book`; they are intentionally simple, not a literal reproduction of
//! the retired market_wing Book.

use anyhow::{Context, Result};
use chrono::NaiveDate;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::Path;

/// Default drawdown halt thresholds (fraction of the account peak).
pub const ACCOUNT_HALT_DD: f64 = 0.50;
pub const CITY_HALT_DD: f64 = 0.25;

/// Per-event-date entry counter — enforces the daily cap across cities.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct DailyCounter {
    pub date: Option<NaiveDate>,
    pub count: u32,
}

/// Per-city realized-P&L track + latched halt.
#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CityState {
    pub cum_pnl: f64,
    pub hwm_pnl: f64,
    pub halted: bool,
}

/// One open (entered, not yet settled) position — also the rich row the engine logs.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct OpenPosition {
    pub captured_utc: String,
    pub market: String,
    pub city: String,
    pub series: String,
    pub event_ticker: String,
    pub ticker: String,
    pub subtitle: String,
    pub event_date: String,
    pub anchor_utc: String,
    pub mid: f64,
    pub yes_ask: f64,
    pub stake_usd: f64,
    pub intended_contracts: f64,
    pub fillable_contracts: f64,
    pub fill_vwap: Option<f64>,
    pub fillable_pct: f64,
    pub ladder_depth_usd: f64,
    pub mode: String,
    pub order_id: Option<String>,
    pub settled: bool,
    pub win: Option<bool>,
    pub pnl_usd: Option<f64>,
}

/// One market's full state.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Book {
    pub bankroll_usd: f64,
    pub account_hwm: f64,
    pub halted_account: bool,
    #[serde(default)]
    pub cities: HashMap<String, CityState>,
    #[serde(default)]
    pub counter: DailyCounter,
    #[serde(default)]
    pub open: Vec<OpenPosition>,
    #[serde(default)]
    pub settled_total: u64,
}

impl Book {
    pub fn new(init_bankroll: f64) -> Self {
        Self {
            bankroll_usd: init_bankroll,
            account_hwm: init_bankroll,
            halted_account: false,
            cities: HashMap::new(),
            counter: DailyCounter::default(),
            open: Vec::new(),
            settled_total: 0,
        }
    }

    /// Entries already taken for `date` (0 if the counter is for another day).
    pub fn entries_today(&self, date: NaiveDate) -> u32 {
        if self.counter.date == Some(date) {
            self.counter.count
        } else {
            0
        }
    }

    /// Record one entry for `date` (resetting the counter on a new day).
    pub fn record_entry(&mut self, date: NaiveDate) {
        if self.counter.date != Some(date) {
            self.counter = DailyCounter { date: Some(date), count: 0 };
        }
        self.counter.count += 1;
    }

    /// Total cost (capital at risk) of this market's open, unsettled positions — `contracts × VWAP`,
    /// falling back to `stake_usd` when no fill price is recorded. Feeds the aggregate exposure cap.
    pub fn open_exposure_usd(&self) -> f64 {
        self.open
            .iter()
            .filter(|p| !p.settled)
            .map(|p| match p.fill_vwap {
                Some(v) if v > 0.0 => p.fillable_contracts * v,
                _ => p.stake_usd,
            })
            .sum()
    }

    pub fn account_halted(&self) -> bool {
        self.halted_account
    }

    pub fn city_halted(&self, city: &str) -> bool {
        self.cities.get(city).map(|c| c.halted).unwrap_or(false)
    }

    /// Book a settled pick's P&L: roll bankroll, update account + per-city high-water marks, and
    /// latch a halt if drawdown breaches the threshold. Returns the new bankroll.
    pub fn book_settlement(&mut self, city: &str, pnl: f64) -> f64 {
        self.bankroll_usd += pnl;
        self.settled_total += 1;
        if self.bankroll_usd > self.account_hwm {
            self.account_hwm = self.bankroll_usd;
        }
        if self.bankroll_usd < self.account_hwm * (1.0 - ACCOUNT_HALT_DD) {
            self.halted_account = true;
        }
        let cs = self.cities.entry(city.to_string()).or_default();
        cs.cum_pnl += pnl;
        if cs.cum_pnl > cs.hwm_pnl {
            cs.hwm_pnl = cs.cum_pnl;
        }
        // per-city drawdown measured in dollars against the account peak
        if (cs.hwm_pnl - cs.cum_pnl) >= CITY_HALT_DD * self.account_hwm {
            cs.halted = true;
        }
        self.bankroll_usd
    }
}

/// Top-level engine state: one `Book` per market + the processed-anchor dedupe set (pruned daily).
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct EngineState {
    pub high: Book,
    pub low: Book,
    #[serde(default)]
    pub processed: Vec<String>,
    #[serde(default)]
    pub processed_date: Option<NaiveDate>,
    /// Single shared LIVE sizing bankroll = real balance × `live_alloc_frac`, adopted at activation
    /// and resynced after each settlement (lesson #11; the exchange is truth). `None` in
    /// paper/dry-run, where the per-market `Book.bankroll_usd` is used instead. One figure for both
    /// markets because LIVE draws on one real Kalshi account. (Per-market Book bankrolls still roll
    /// for the orthogonal drawdown halts.)
    #[serde(default)]
    pub live_bankroll: Option<f64>,
    /// Write-ahead journal of an in-flight LIVE order (G3): the position we're about to place,
    /// persisted *before* the order is sent so a crash between the place and the post-place save is
    /// recoverable. Reconciliation (G4) adopts it — with the exchange's true fill — on the next
    /// cycle/boot. `None` whenever no order is in flight. Live-only.
    #[serde(default)]
    pub pending: Option<OpenPosition>,
}

impl EngineState {
    pub fn new(init_bankroll: f64) -> Self {
        Self {
            high: Book::new(init_bankroll),
            low: Book::new(init_bankroll),
            processed: Vec::new(),
            processed_date: None,
            live_bankroll: None,
            pending: None,
        }
    }

    /// Load from disk; `Ok(None)` if the file is absent (caller starts fresh), `Err` if it exists
    /// but is unreadable/corrupt (never silently reset a money bot's bankroll).
    pub fn load(path: &Path) -> Result<Option<Self>> {
        if !path.exists() {
            return Ok(None);
        }
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading state {}", path.display()))?;
        let s: Self = serde_json::from_str(&text)
            .with_context(|| format!("parsing state {}", path.display()))?;
        Ok(Some(s))
    }

    /// Atomic write: serialize to `<path>.tmp` then rename over `path`.
    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir).ok();
        }
        let tmp = path.with_extension("tmp");
        std::fs::write(&tmp, serde_json::to_string_pretty(self)?)
            .with_context(|| format!("writing {}", tmp.display()))?;
        std::fs::rename(&tmp, path).with_context(|| format!("renaming into {}", path.display()))?;
        Ok(())
    }

    pub fn book(&mut self, market: &str) -> &mut Book {
        if market == "high" {
            &mut self.high
        } else {
            &mut self.low
        }
    }

    /// True if an *unsettled* open position for `ticker` exists in either market. Live reconciliation
    /// uses this to tell a tracked fill from an orphan exchange holding that needs adopting.
    pub fn is_tracked(&self, ticker: &str) -> bool {
        self.high.open.iter().chain(self.low.open.iter()).any(|p| p.ticker == ticker && !p.settled)
    }

    /// True if this anchor key was already processed for `date`. Non-mutating: querying a different
    /// date returns false without disturbing the stored set (the date-roll clear lives in
    /// `mark_processed`), so peeking at tomorrow's anchors can't forget today's processed set.
    pub fn is_processed(&self, key: &str, date: NaiveDate) -> bool {
        self.processed_date == Some(date) && self.processed.iter().any(|k| k == key)
    }

    pub fn mark_processed(&mut self, key: String, date: NaiveDate) {
        if self.processed_date != Some(date) {
            self.processed.clear();
            self.processed_date = Some(date);
        }
        if !self.processed.contains(&key) {
            self.processed.push(key);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn d(y: i32, m: u32, day: u32) -> NaiveDate {
        NaiveDate::from_ymd_opt(y, m, day).unwrap()
    }

    fn open_pos(ticker: &str, settled: bool) -> OpenPosition {
        OpenPosition {
            captured_utc: "t".into(),
            market: "high".into(),
            city: "Chicago".into(),
            series: "KXHIGHCHI".into(),
            event_ticker: "KXHIGHCHI-26JUN16".into(),
            ticker: ticker.into(),
            subtitle: String::new(),
            event_date: "2026-06-16".into(),
            anchor_utc: String::new(),
            mid: 0.94,
            yes_ask: 0.95,
            stake_usd: 41.0,
            intended_contracts: 43.0,
            fillable_contracts: 40.0,
            fill_vwap: Some(0.95),
            fillable_pct: 1.0,
            ladder_depth_usd: 0.0,
            mode: "live".into(),
            order_id: None,
            settled,
            win: None,
            pnl_usd: None,
        }
    }

    #[test]
    fn is_tracked_only_counts_unsettled() {
        let mut s = EngineState::new(250.0);
        assert!(!s.is_tracked("T-X"));
        s.high.open.push(open_pos("T-X", false));
        assert!(s.is_tracked("T-X")); // tracked in high
        s.low.open.push(open_pos("T-Y", true));
        assert!(!s.is_tracked("T-Y")); // settled -> not tracked
    }

    #[test]
    fn open_exposure_sums_unsettled_cost() {
        let mut b = Book::new(250.0);
        assert_eq!(b.open_exposure_usd(), 0.0);
        b.open.push(open_pos("T-A", false)); // 40 × 0.95 = 38.0
        b.open.push(open_pos("T-B", true)); // settled -> excluded
        assert!((b.open_exposure_usd() - 38.0).abs() < 1e-9);
    }

    #[test]
    fn pending_journal_roundtrips_through_disk() {
        let mut s = EngineState::new(250.0);
        s.pending = Some(open_pos("T-P", false));
        let path = std::env::temp_dir().join("wa_state_pending_test.json");
        s.save(&path).unwrap();
        let loaded = EngineState::load(&path).unwrap().unwrap();
        assert_eq!(loaded.pending.unwrap().ticker, "T-P");
        std::fs::remove_file(&path).ok();
    }

    #[test]
    fn daily_counter_resets_on_new_day() {
        let mut b = Book::new(250.0);
        let t1 = d(2026, 6, 15);
        assert_eq!(b.entries_today(t1), 0);
        b.record_entry(t1);
        b.record_entry(t1);
        assert_eq!(b.entries_today(t1), 2);
        // new day -> counter resets
        let t2 = d(2026, 6, 16);
        assert_eq!(b.entries_today(t2), 0);
        b.record_entry(t2);
        assert_eq!(b.entries_today(t2), 1);
    }

    #[test]
    fn account_halt_latches_at_50pct_drawdown() {
        let mut b = Book::new(250.0);
        b.book_settlement("Chicago", -100.0); // 250 -> 150, dd 40% (< 50)
        assert!(!b.account_halted());
        b.book_settlement("Chicago", -30.0); // 150 -> 120, dd 52% from peak 250
        assert!(b.account_halted());
    }

    #[test]
    fn city_halt_latches_at_25pct_drawdown() {
        let mut b = Book::new(250.0);
        b.book_settlement("Austin", 40.0); // city hwm 40
        b.book_settlement("Austin", -25.0); // city cum 15; dd 25 < 0.25*account_hwm(290)=72.5 -> ok
        assert!(!b.city_halted("Austin"));
        b.book_settlement("Austin", -60.0); // cum -45; dd 85 >= 72.5 -> halt
        assert!(b.city_halted("Austin"));
    }

    #[test]
    fn state_roundtrips_through_disk() {
        let mut s = EngineState::new(250.0);
        s.high.record_entry(d(2026, 6, 15));
        s.mark_processed("high|KXHIGHCHI|2026-06-15".into(), d(2026, 6, 15));
        let path = std::env::temp_dir().join("wa_state_roundtrip_test.json");
        s.save(&path).unwrap();
        let loaded = EngineState::load(&path).unwrap().unwrap();
        assert_eq!(loaded.high.entries_today(d(2026, 6, 15)), 1);
        assert!(loaded.processed.contains(&"high|KXHIGHCHI|2026-06-15".to_string()));
        std::fs::remove_file(&path).ok();
    }
}
