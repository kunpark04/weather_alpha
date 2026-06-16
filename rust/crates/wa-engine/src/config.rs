//! Engine configuration (`wa.toml`) + construction of the runtime city list.

use anyhow::{Context, Result};
use serde::Deserialize;
use std::path::{Path, PathBuf};
use wa_algo::AlgoCfg;
use wa_schedule::City;

#[derive(Debug, Deserialize)]
pub struct Config {
    #[serde(default = "default_api_base")]
    pub api_base: String,
    /// Per-market starting bankroll (high and low are independent strategies, like the paper bot).
    pub bankroll_init: f64,
    /// "paper" | "live" | "dry-run" — overridden by a CLI flag if present.
    #[serde(default = "default_mode")]
    pub mode: String,
    pub state_path: PathBuf,
    pub log_path: PathBuf,
    #[serde(default = "default_kill")]
    pub kill_switch_path: PathBuf,
    #[serde(default = "default_key_id_env")]
    pub key_id_env: String,
    #[serde(default = "default_key_path_env")]
    pub private_key_path_env: String,
    #[serde(default = "default_window")]
    pub anchor_window_min: i64,
    #[serde(default = "default_poll")]
    pub poll_interval_sec: u64,
    /// Fraction of the real Kalshi balance to allocate as the single shared LIVE sizing bankroll
    /// (set at deploy; 1.0 = the whole account, 0.5 = half). Both markets size off this one figure.
    /// Paper/dry-run ignore it (they use the per-market `bankroll_init`).
    #[serde(default = "default_live_alloc")]
    pub live_alloc_frac: f64,
    /// Aggregate exposure cap: refuse a new entry if total open cost would exceed this fraction of
    /// the sizing bankroll (account-wide in live / per-market in paper). A backstop on capital at
    /// risk beyond the daily entry count; 1.0 = up to the whole bankroll.
    #[serde(default = "default_exposure_max")]
    pub total_exposure_max_pct: f64,
    pub algo: AlgoToml,
    pub cities: Vec<CityToml>,
}

#[derive(Debug, Deserialize)]
pub struct AlgoToml {
    pub band_lo: f64,
    pub band_hi: f64,
    pub daily_cap: u32,
    pub stake_fraction: f64,
    pub cap_price: f64,
}

#[derive(Debug, Deserialize)]
pub struct CityToml {
    pub name: String,
    pub series_high: String,
    pub series_low: String,
    pub tz: String,
}

fn default_api_base() -> String {
    wa_kalshi::DEFAULT_API_BASE.to_string()
}
fn default_mode() -> String {
    "paper".into()
}
fn default_kill() -> PathBuf {
    PathBuf::from("KILL_SWITCH")
}
fn default_key_id_env() -> String {
    "KALSHI_KEY_ID".into()
}
fn default_key_path_env() -> String {
    "KALSHI_PRIVATE_KEY_PATH".into()
}
fn default_window() -> i64 {
    60
}
fn default_poll() -> u64 {
    300
}
fn default_live_alloc() -> f64 {
    1.0
}
fn default_exposure_max() -> f64 {
    1.0
}

impl Config {
    pub fn load(path: &Path) -> Result<Self> {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("reading config {}", path.display()))?;
        let cfg: Config =
            toml::from_str(&text).with_context(|| format!("parsing config {}", path.display()))?;
        Ok(cfg)
    }

    pub fn algo(&self) -> AlgoCfg {
        AlgoCfg {
            band_lo: self.algo.band_lo,
            band_hi: self.algo.band_hi,
            daily_cap: self.algo.daily_cap,
            stake_fraction: self.algo.stake_fraction,
            cap_price: self.algo.cap_price,
        }
    }

    /// Build the scheduler city list, parsing each IANA timezone.
    pub fn cities(&self) -> Result<Vec<City>> {
        self.cities
            .iter()
            .map(|c| {
                let tz = c
                    .tz
                    .parse()
                    .map_err(|e| anyhow::anyhow!("city {}: bad timezone {:?}: {e}", c.name, c.tz))?;
                Ok(City {
                    name: c.name.clone(),
                    series_high: c.series_high.clone(),
                    series_low: c.series_low.clone(),
                    tz,
                })
            })
            .collect()
    }
}
