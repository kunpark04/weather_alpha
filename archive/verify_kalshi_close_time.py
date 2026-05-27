"""Verify Kalshi close_time for KXHIGHCHI (KMDW daily-high) markets.

Hits the public no-auth API, lists recent settled markets, prints the
close_time and a few sibling fields for each. Used to confirm the "midnight
local on settlement day" assumption that drives the rest of the data pipeline.
"""
import json
import urllib.request
import urllib.parse
import pandas as pd

API_BASE = "https://api.elections.kalshi.com/trade-api/v2"

# Pull a batch of recently settled KXHIGHCHI markets. status=settled gives the
# clean past view; trade-history is not needed for the close_time check.
params = {
    "series_ticker": "KXHIGHCHI",
    "status":        "settled",
    "limit":         50,
}
url = f"{API_BASE}/markets?{urllib.parse.urlencode(params)}"
print(f"GET {url}\n")

req = urllib.request.Request(url, headers={"User-Agent": "forecast-alpha-research/0.1"})
with urllib.request.urlopen(req, timeout=30) as resp:
    body = json.loads(resp.read())

markets = body.get("markets", [])
print(f"Returned {len(markets)} markets.")
if not markets:
    print("No markets returned. Raw payload:")
    print(json.dumps(body, indent=2)[:1000])
    raise SystemExit(1)

# Show the first market's full record so we know what fields exist
print("\n--- First market, full record ---")
print(json.dumps(markets[0], indent=2)[:2500])

# Extract just the fields we care about
df = pd.DataFrame([
    {
        "ticker":         m.get("ticker"),
        "event_ticker":   m.get("event_ticker"),
        "open_time":      m.get("open_time"),
        "close_time":     m.get("close_time"),
        "expected_expiration_time": m.get("expected_expiration_time"),
        "expiration_time":m.get("expiration_time"),
        "settlement_time":m.get("settlement_time"),
        "result":         m.get("result"),
        "subtitle":       m.get("subtitle"),
    }
    for m in markets
])

# Parse timestamps to Chicago local for human-readable comparison to "midnight local"
for col in ("open_time", "close_time", "expected_expiration_time",
            "expiration_time", "settlement_time"):
    df[col] = pd.to_datetime(df[col], utc=True, errors="coerce")
    df[col + "_chi"] = df[col].dt.tz_convert("America/Chicago")

print("\n--- close_time in America/Chicago (the load-bearing column) ---")
print(df[["ticker", "subtitle", "close_time_chi"]].to_string(index=False))

print("\n--- close_time hour-of-day distribution (Chicago local) ---")
close_hours = df["close_time_chi"].dt.hour.dropna()
close_mins  = df["close_time_chi"].dt.minute.dropna()
print(f"Unique hours: {sorted(close_hours.unique().tolist())}")
print(f"Unique (hour, minute) pairs: "
      f"{sorted(set(zip(close_hours, close_mins)))[:10]}")

# Compare close_time vs the event's intended settlement-day midnight
# (subtitle typically encodes the settlement day -- inspect manually if needed)
print("\n--- open_time and close_time delta ---")
df["window_hours"] = (df["close_time"] - df["open_time"]).dt.total_seconds() / 3600
print(df[["ticker", "open_time_chi", "close_time_chi", "window_hours"]]
      .head(10).to_string(index=False))
