//! Per-city local-anchor scheduler — the "Option A true live replication" timing layer.
//!
//! Each city fires its high market at **17:00** and its low market at **22:00** in that city's own
//! local timezone (DST-correct via `chrono-tz`). The scheduler enumerates upcoming anchors; the
//! engine sleeps until the soonest, processes it within a post-anchor window, and (like the Python
//! `MarketAnchorScheduler`) does **not** chase an anchor whose window has already elapsed on a late
//! restart.
//!
//! `upcoming` is O(cities · days); the engine takes the head of the sorted list, so picking the next
//! anchor is effectively O(1) amortized over a day.

use chrono::{DateTime, Datelike, Duration, LocalResult, NaiveDate, TimeZone, Utc};
use chrono_tz::Tz;

/// A tradeable city: the two Kalshi series prefixes + its IANA timezone.
#[derive(Clone, Debug)]
pub struct City {
    pub name: String,
    pub series_high: String,
    pub series_low: String,
    pub tz: Tz,
}

/// Which daily market — fixes the local anchor hour.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Market {
    High,
    Low,
}

impl Market {
    pub fn anchor_hour(self) -> u32 {
        match self {
            Market::High => 17,
            Market::Low => 22,
        }
    }
    pub fn as_str(self) -> &'static str {
        match self {
            Market::High => "high",
            Market::Low => "low",
        }
    }
    pub const ALL: [Market; 2] = [Market::High, Market::Low];
}

/// One concrete anchor firing: city + market + the event's local date + the UTC instant it fires.
#[derive(Clone, Debug)]
pub struct Anchor {
    pub city_idx: usize,
    pub city_name: String,
    pub market: Market,
    pub series: String,
    pub event_date: NaiveDate,
    pub fire_utc: DateTime<Utc>,
}

impl Anchor {
    /// Stable per-day identity (so the engine can dedupe processed anchors across loop iterations).
    pub fn key(&self) -> String {
        format!("{}|{}|{}", self.market.as_str(), self.series, self.event_date)
    }
}

/// UTC instant of `market`'s anchor on local calendar `date` in `city.tz`. 17:00/22:00 never collide
/// with the 02:00 DST transition, so the result is unambiguous; we still handle the edge defensively.
pub fn anchor_utc(city: &City, market: Market, date: NaiveDate) -> Option<DateTime<Utc>> {
    let h = market.anchor_hour();
    match city.tz.with_ymd_and_hms(date.year(), date.month(), date.day(), h, 0, 0) {
        LocalResult::Single(dt) => Some(dt.with_timezone(&Utc)),
        LocalResult::Ambiguous(dt, _) => Some(dt.with_timezone(&Utc)),
        LocalResult::None => None,
    }
}

/// All anchors firing at or after `since`, within `since .. since + days`, sorted by fire time.
/// Pass `since = now - window` so an anchor that fired moments ago (still inside its window) is
/// included. The local event-date is the anchor's own local calendar date.
pub fn upcoming(cities: &[City], since: DateTime<Utc>, days: i64) -> Vec<Anchor> {
    let start = since.date_naive() - Duration::days(1); // local dates can lead/lag the UTC date
    let mut out = Vec::new();
    for off in 0..=days {
        let date = start + Duration::days(off);
        for (i, city) in cities.iter().enumerate() {
            for market in Market::ALL {
                let series = match market {
                    Market::High => &city.series_high,
                    Market::Low => &city.series_low,
                };
                if series.is_empty() {
                    continue; // e.g. NYC has no KXLOWT low series
                }
                if let Some(fire) = anchor_utc(city, market, date) {
                    if fire >= since {
                        out.push(Anchor {
                            city_idx: i,
                            city_name: city.name.clone(),
                            market,
                            series: series.clone(),
                            event_date: date,
                            fire_utc: fire,
                        });
                    }
                }
            }
        }
    }
    out.sort_by_key(|a| a.fire_utc);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn chicago() -> City {
        City {
            name: "Chicago".into(),
            series_high: "KXHIGHCHI".into(),
            series_low: "KXLOWTCHI".into(),
            tz: "America/Chicago".parse().unwrap(),
        }
    }

    #[test]
    fn cdt_anchors_convert_correctly() {
        let c = chicago();
        let date = NaiveDate::from_ymd_opt(2026, 6, 15).unwrap();
        // June -> CDT (UTC-5): 17:00 local = 22:00 UTC same day
        let hi = anchor_utc(&c, Market::High, date).unwrap();
        assert_eq!(hi.to_rfc3339(), "2026-06-15T22:00:00+00:00");
        // 22:00 local = 03:00 UTC next day
        let lo = anchor_utc(&c, Market::Low, date).unwrap();
        assert_eq!(lo.to_rfc3339(), "2026-06-16T03:00:00+00:00");
    }

    #[test]
    fn cst_anchors_shift_by_dst() {
        let c = chicago();
        let date = NaiveDate::from_ymd_opt(2026, 1, 15).unwrap();
        // January -> CST (UTC-6): 17:00 local = 23:00 UTC
        let hi = anchor_utc(&c, Market::High, date).unwrap();
        assert_eq!(hi.to_rfc3339(), "2026-01-15T23:00:00+00:00");
    }

    #[test]
    fn upcoming_is_sorted_and_future_only() {
        let cities = vec![chicago()];
        let now = Utc.with_ymd_and_hms(2026, 6, 15, 21, 0, 0).unwrap(); // 16:00 CDT
        let up = upcoming(&cities, now, 2);
        assert!(!up.is_empty());
        // first upcoming = today's high at 22:00 UTC
        assert_eq!(up[0].market, Market::High);
        assert_eq!(up[0].fire_utc.to_rfc3339(), "2026-06-15T22:00:00+00:00");
        // sorted ascending
        assert!(up.windows(2).all(|w| w[0].fire_utc <= w[1].fire_utc));
        // all strictly at/after `since`
        assert!(up.iter().all(|a| a.fire_utc >= now));
    }
}
