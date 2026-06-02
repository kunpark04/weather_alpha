"""Running-max ratchet feasibility probe — strat_prod_2.

Thesis: the day's max temperature is monotonically non-decreasing. Once the running
max clears a bucket's UPPER bound, that bucket can never win => its true YES value is 0.
If the Kalshi book LAGS in repricing dead buckets to ~0, you can SELL YES (hit the resting
yes_bid) on a bucket worth 0 and lock the bid, minus taker fee. The edge = the size and
duration of that lag.

This probe time-aligns two streams for one city/day:
  * intraday temperature obs (api.weather.gov METAR/SPECI) -> running max -> per-bucket KILL time
  * the orderbook .jsonl snapshots (scripts/orderbook_logger.py) -> yes_bid after the kill

For each bucket that the running max kills, it reports:
  kill_F / kill_time      the temp + UTC time the bucket became dead (true value 0)
  best_bid_after_c        the highest yes_bid (cents) seen AT/AFTER the kill -> sellable edge
  lag_min                 minutes from kill until yes_bid fell to <=1c (mispricing duration)
  harvest_c               time-avg (yes_bid - taker_fee) while sellable -> per-contract net
  sellable_size           median yes_bid_size while sellable -> contracts you could lift

CAVEATS: api.weather.gov obs are ~hourly+specials (coarse vs the minutes-scale lag we want;
1-min IEM ASOS lags 24-48h so isn't available same-day). Settlement is the NWS CLI integer
daily max -> small basis risk vs these obs. One day, partial (pre-afternoon-peak). Mechanics
read, not a statistical verdict.

Usage:
  python scripts/ratchet_probe.py <station> <book-dir> [YYYY-MM-DD]
  e.g. python scripts/ratchet_probe.py KMDW ...\\KXHIGHCHI-2026-06-02 2026-06-02
"""
from __future__ import annotations

import json
import math
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

WEATHER_GOV = "https://api.weather.gov"
UA = {"User-Agent": "weather-alpha-ratchet-probe/1.0 (research)", "Accept": "application/geo+json"}


def fetch_obs(station: str, day: str, limit: int = 500) -> list[tuple[datetime, float]]:
    """[(utc_ts, temp_F)] for `station` on calendar `day` (UTC), sorted ascending."""
    url = f"{WEATHER_GOV}/stations/{station}/observations?limit={limit}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        feats = json.loads(r.read().decode("utf-8")).get("features", [])
    out = []
    for ft in feats:
        p = ft.get("properties", {})
        ts, tv = p.get("timestamp"), (p.get("temperature") or {}).get("value")
        if not ts or tv is None:
            continue
        t = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        if t.strftime("%Y-%m-%d") != day:
            continue
        out.append((t, tv * 9.0 / 5.0 + 32.0))  # api.weather.gov temp is degC
    return sorted(out)


def bucket_upper_from_ticker(ticker: str, t_strikes: list[int]) -> float | None:
    """Upper integer °F a bucket still WINS at, parsed from the ticker suffix (subtitles
    aren't always logged). None => top (>=) tail, never heat-killed.

      -B74.5 -> range [74,75], U=75 (dead once max>=76)
      -T72 (the LOWER of the two T strikes) -> '<=71', U=71 (dead once max>=72)
      -T79 (the HIGHER T strike) -> '>=80' top tail -> None
    """
    suf = ticker.split("-")[-1]
    if suf.startswith("B"):
        lo = math.floor(float(suf[1:]))
        return float(lo + 1)                 # [lo, lo+1] wins up to lo+1
    if suf.startswith("T") and t_strikes:
        n = int(suf[1:])
        if n == min(t_strikes):
            return float(n - 1)              # low tail '<= n-1'
        return None                          # high tail '>= n+1' (not heat-killed)
    return None


def load_book(fp: Path) -> list[dict]:
    rows = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            t = datetime.fromisoformat(r["ts"]).astimezone(timezone.utc)
        except (KeyError, ValueError):
            continue
        def _f(x):
            try:
                return float(x)
            except (TypeError, ValueError):
                return None
        rows.append({"t": t, "subtitle": r.get("subtitle"),
                     "yes_bid": _f(r.get("yes_bid")), "yes_ask": _f(r.get("yes_ask")),
                     "yes_bid_size": _f(r.get("yes_bid_size"))})
    return sorted(rows, key=lambda x: x["t"])


def taker_fee_c(p: float) -> float:
    return 7.0 * max(0.0, min(1.0, p)) * (1.0 - max(0.0, min(1.0, p)))


def running_max_at(obs: list[tuple[datetime, float]], t: datetime) -> float | None:
    """Running max of rounded-°F obs at/before time t."""
    vals = [round(f) for (ot, f) in obs if ot <= t]
    return float(max(vals)) if vals else None


def analyze(station: str, book_dir: Path, day: str):
    obs = fetch_obs(station, day)
    if not obs:
        print(f"!! no obs for {station} on {day} (api.weather.gov)")
        return
    day_max = max(round(f) for _, f in obs)
    print(f"== {station} {day} ==  obs={len(obs)}  "
          f"first={obs[0][0].strftime('%H:%MZ')} {obs[0][1]:.0f}F  "
          f"last={obs[-1][0].strftime('%H:%MZ')} {obs[-1][1]:.0f}F  running-max-so-far={day_max}F")
    print("  (note: if last obs is pre-afternoon, the day's true peak is still ahead)\n")

    files = sorted(book_dir.glob("*.jsonl"))
    t_strikes = [int(fp.stem.split("-")[-1][1:]) for fp in files if fp.stem.split("-")[-1].startswith("T")]

    hdr = (f"{'bucket':<26}{'U_F':>5}{'killF':>6}{'obs_kill':>9}{'mkt_dead':>9}"
           f"{'mkt-obs_m':>10}{'bestbid_c':>10}{'harvest_c':>10}{'sellsz':>8}{'verdict':>11}")
    print(hdr); print("-" * len(hdr))

    for fp in files:
        rows = load_book(fp)
        if not rows:
            continue
        U = bucket_upper_from_ticker(fp.stem, t_strikes)
        if U is None:
            print(f"{fp.stem:<26}{'  >=':>5}   (top tail: long-confirm candidate, not a short)")
            continue
        kill_F = U + 1  # max reaches this => bucket can't win

        # obs side: first snapshot where the running max has killed the bucket
        kill_row = next((r for r in rows if (rm := running_max_at(obs, r["t"])) is not None and rm >= kill_F), None)
        # market side: time after which yes_bid stays <= 1c for good (the book's own "dead" verdict)
        mkt_dead = None
        last_alive = None
        for r in rows:
            if (r["yes_bid"] or 0.0) > 0.01:
                last_alive = r["t"]
        if last_alive is None:
            mkt_dead = rows[0]["t"]                       # never bid > 1c in window
        else:
            after_alive = [r["t"] for r in rows if r["t"] > last_alive]
            mkt_dead = after_alive[0] if after_alive else None  # None => still bidding at end

        if kill_row is None:
            ob = "n/a"; ks = "   -"; mkt_obs = float("nan")
            print(f"{fp.stem:<26}{U:>5.0f}{kill_F:>6.0f}{ks:>9}"
                  f"{(mkt_dead.strftime('%H:%MZ') if mkt_dead else 'alive'):>9}{'  obs-not-killed':>10}")
            continue
        kt = kill_row["t"]

        # harvestable window: obs says dead, but market still bids > 1c
        sellable = [r for r in rows if r["t"] >= kt and (r["yes_bid"] or 0.0) > 0.01]
        best_bid_c = max((r["yes_bid"] * 100) for r in sellable) if sellable else 0.0
        harvest_c = (sum((r["yes_bid"] * 100 - taker_fee_c(r["yes_bid"])) for r in sellable) / len(sellable)) if sellable else 0.0
        sizes = sorted((r["yes_bid_size"] or 0.0) for r in sellable)
        sellsz = sizes[len(sizes)//2] if sizes else 0.0
        mkt_obs_min = ((mkt_dead - kt).total_seconds() / 60.0) if mkt_dead else float("inf")

        verdict = "EDGE" if (best_bid_c >= 2.0 and len(sellable) >= 2 and sellsz >= 1) else "no/anticip"
        print(f"{fp.stem:<26}{U:>5.0f}{kill_F:>6.0f}{kt.strftime('%H:%MZ'):>9}"
              f"{(mkt_dead.strftime('%H:%MZ') if mkt_dead else 'alive'):>9}"
              f"{mkt_obs_min:>10.0f}{best_bid_c:>10.1f}{harvest_c:>10.2f}{sellsz:>8.0f}{verdict:>11}")

    print("\nmkt-obs_m = minutes the market's repricing-to-dead came AFTER the obs killed the bucket.")
    print("  positive => book LAGGED obs (harvestable); negative/~0 => market ANTICIPATED (no ratchet edge).")
    print("EDGE = sellable bid >=2c for >=2 snaps with >=1 contract resting, in the post-obs-kill window.")


def main(argv):
    if len(argv) < 2:
        print(__doc__); return 2
    station, book_dir = argv[0], Path(argv[1])
    day = argv[2] if len(argv) > 2 else book_dir.name[-10:]
    analyze(station, book_dir, day)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
