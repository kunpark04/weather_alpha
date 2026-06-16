"""Forward PAPER-trade the two locked directional configs, FOLLOWING LIVE TRADING BEHAVIOR.

This bot now fires PER (timezone, market) at that timezone's local anchor (high 17:00 / low 22:00)
and reads the **LIVE Kalshi order book AT THAT MOMENT** (keyless public API) — exactly what a live
bot sees — instead of reading the logged orderbook tape after the fact. It mirrors the Rust LIVE
engine's executable rule one-for-one so the paper results predict live behavior:

  * favorite = highest-mid bucket; ENTER iff its mid ∈ [0.93, 0.95];
  * daily cap of NCITIES=3 entries **per market per event-date**, accumulated across the tz firings
    as anchors elapse east→west (first-come — NO cross-city look-ahead, unlike the backtest's
    "top-3 by confidence"); within one tz's simultaneous batch, config (dict) order breaks ties,
    matching the Rust scheduler;
  * each entry stakes one of 3 equal slots = STAKE/NCITIES = 16.67% of the bankroll;
  * P&L booked at the achievable fill (VWAP from walking the live depth ladder), net of fees.

PAPER only (no orders). Settlement is keyless. Designed to be invoked by per-(tz,market) systemd
timers; settlement by a separate morning timer.

Usage (one tz+market per firing):
  python scripts/directional_paper.py --market high --tz America/New_York --capture
  python scripts/directional_paper.py --market low  --tz America/Chicago  --capture
  python scripts/directional_paper.py --market both --settle        # book outcomes (any time)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
OUTDIR = _ROOT / "data" / "directional_paper"
BAND = (0.93, 0.95)
NCITIES = 3               # daily cap of entries per market per event-date (the "top-3")
STAKE = 0.50              # total fraction of bankroll for the day, split into NCITIES equal slots
CAP = 0.97               # walk the ask ladder only up to this price (edge gone above)
FEE = lambda p: 0.07 * p * (1 - p)
INIT_BANKROLL = 250.0
API = "https://api.elections.kalshi.com/trade-api/v2"
_MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

# series -> (label, IANA tz, climate region). high uses KXHIGH*, low uses KXLOWT* (no NY low).
HIGH = {
    "KXHIGHNY": ("NYC", "America/New_York", "NE"), "KXHIGHCHI": ("Chicago", "America/Chicago", "MW"),
    "KXHIGHMIA": ("Miami", "America/New_York", "GulfSE"), "KXHIGHAUS": ("Austin", "America/Chicago", "TX"),
    "KXHIGHDEN": ("Denver", "America/Denver", "Mtn"), "KXHIGHPHIL": ("Philadelphia", "America/New_York", "NE"),
    "KXHIGHLAX": ("Los Angeles", "America/Los_Angeles", "WC"), "KXHIGHTLV": ("Las Vegas", "America/Los_Angeles", "Desert"),
    "KXHIGHTNOLA": ("New Orleans", "America/Chicago", "GulfSE"), "KXHIGHTSEA": ("Seattle", "America/Los_Angeles", "WC"),
    "KXHIGHTSFO": ("San Francisco", "America/Los_Angeles", "WC"), "KXHIGHTDC": ("Washington DC", "America/New_York", "NE"),
    "KXHIGHTATL": ("Atlanta", "America/New_York", "GulfSE"), "KXHIGHTMIN": ("Minneapolis", "America/Chicago", "MW"),
    "KXHIGHTPHX": ("Phoenix", "America/Phoenix", "Desert"), "KXHIGHTBOS": ("Boston", "America/New_York", "NE"),
    "KXHIGHTDAL": ("Dallas", "America/Chicago", "TX"), "KXHIGHTOKC": ("Oklahoma City", "America/Chicago", "Plains"),
    "KXHIGHTSATX": ("San Antonio", "America/Chicago", "TX"), "KXHIGHTHOU": ("Houston", "America/Chicago", "TX"),
}
LOW = {
    "KXLOWTCHI": HIGH["KXHIGHCHI"], "KXLOWTMIA": HIGH["KXHIGHMIA"], "KXLOWTAUS": HIGH["KXHIGHAUS"],
    "KXLOWTDEN": HIGH["KXHIGHDEN"], "KXLOWTPHIL": HIGH["KXHIGHPHIL"], "KXLOWTLAX": HIGH["KXHIGHLAX"],
    "KXLOWTLV": HIGH["KXHIGHTLV"], "KXLOWTNOLA": HIGH["KXHIGHTNOLA"], "KXLOWTSEA": HIGH["KXHIGHTSEA"],
    "KXLOWTSFO": HIGH["KXHIGHTSFO"], "KXLOWTDC": HIGH["KXHIGHTDC"], "KXLOWTATL": HIGH["KXHIGHTATL"],
    "KXLOWTMIN": HIGH["KXHIGHTMIN"], "KXLOWTPHX": HIGH["KXHIGHTPHX"], "KXLOWTBOS": HIGH["KXHIGHTBOS"],
    "KXLOWTDAL": HIGH["KXHIGHTDAL"], "KXLOWTOKC": HIGH["KXHIGHTOKC"], "KXLOWTSATX": HIGH["KXHIGHTSATX"],
    "KXLOWTHOU": HIGH["KXHIGHTHOU"],
}
ANCHOR_HM = {"high": (17, 0), "low": (22, 0)}


def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def datecode(ds: str) -> str:
    """Kalshi event-ticker date code from 'YYYY-MM-DD' (e.g. '2026-06-15' -> '26JUN15')."""
    return f"{ds[2:4]}{_MON[int(ds[5:7]) - 1]}{ds[8:10]}"


def http_json(url, retries=6):
    """Keyless GET with 429/5xx-aware backoff — the public tier is shared with the orderbook logger
    on the droplet, and a same-tz fan-out (CT has 8 cities) can 429 without it."""
    delay, last = 0.5, None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "wa-paper/2.0", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if (e.code == 429 or e.code >= 500) and attempt + 1 < retries:
                ra = e.headers.get("Retry-After") if e.headers else None
                try:
                    wait = float(ra) if ra else delay
                except (TypeError, ValueError):
                    wait = delay
                time.sleep(min(wait, 3.0)); delay *= 2; continue
            raise
        except Exception as e:  # transport hiccup -> retry
            last = e
            if attempt + 1 >= retries:
                raise
            time.sleep(delay); delay *= 2
    raise last  # pragma: no cover


def fetch_markets(event_ticker: str) -> list[dict]:
    try:
        return http_json(f"{API}/markets?event_ticker={event_ticker}&limit=100").get("markets", []) or []
    except Exception as e:  # one city's fetch must not sink the others
        print(f"   ! fetch_markets {event_ticker} failed: {e}")
        return []


def fetch_orderbook(ticker: str) -> dict:
    """Return {'yes': [[price_dollars, size], ...], 'no': [...]} (dollar prices).
    Kalshi serves the book under 'orderbook_fp' with dollar-denominated
    'yes_dollars'/'no_dollars' (exactly what orderbook_logger.py reads); the legacy
    'orderbook' shape was cents. Handle both so the depth ladder always populates."""
    try:
        j = http_json(f"{API}/markets/{ticker}/orderbook")
    except Exception as e:
        print(f"   ! fetch_orderbook {ticker} failed: {e}")
        return {"yes": [], "no": []}
    fp = j.get("orderbook_fp")
    if isinstance(fp, dict):  # current API: already-dollar 'yes_dollars'/'no_dollars'
        return {"yes": _coerce_levels(fp.get("yes_dollars")), "no": _coerce_levels(fp.get("no_dollars"))}
    ob = j.get("orderbook", {}) or {}  # legacy cents shape -> scale down
    return {"yes": _book_dollars(ob.get("yes")), "no": _book_dollars(ob.get("no"))}


def _coerce_levels(levels) -> list:
    """[[price, size], ...] (str or num) -> [[float_price, float_size], ...], NO scaling
    (orderbook_fp prices are already dollars)."""
    out = []
    for lvl in (levels or []):
        if isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            p, sz = _f(lvl[0]), _f(lvl[1])
            if p is not None and sz is not None:
                out.append([round(p, 4), sz])
    return out


def _book_dollars(levels) -> list:
    """Legacy cents ladder [[price_cents, size], ...] -> [[price_dollars, size], ...]."""
    return [[round(p / 100.0, 4), sz] for p, sz in _coerce_levels(levels)]


def rec_from_market(m: dict) -> dict:
    """Build a snapshot rec (dollar prices) from a live /markets object — superset of fields, so the
    log captures everything available ('better safe than sorry'). Book/sizes are filled per-favorite."""
    g = lambda *ks: next((v for v in (_f(m.get(k)) for k in ks) if v is not None), None)
    return {
        "ticker": m.get("ticker"), "event": m.get("event_ticker"),
        "yes_bid": g("yes_bid_dollars"), "yes_ask": g("yes_ask_dollars"),
        "no_bid": g("no_bid_dollars"), "no_ask": g("no_ask_dollars"),
        "last": g("last_price_dollars"),
        "yes_bid_size": None, "yes_ask_size": None,  # top-of-book sizes come from the orderbook
        "vol": g("volume"), "vol_24h": g("volume_24h", "volume_24h_fp"),
        "open_interest": g("open_interest", "open_interest_fp"),
        "liquidity": g("liquidity_dollars", "liquidity"),
        "status": m.get("status"),
        "subtitle": m.get("subtitle") or m.get("yes_sub_title"),
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "yes_book": None, "no_book": None,
    }


def _midp(r):
    b, a = _f(r.get("yes_bid")), _f(r.get("yes_ask"))
    return (b + a) / 2 if (b is not None and a is not None) else (_f(r.get("last")) or a or 0.0)


def yes_ask_ladder(rec: dict) -> list[tuple]:
    """Ascending (yes_ask_price, size) ladder to BUY yes = the no-book (resting no bids) flipped."""
    out = []
    for lvl in (rec.get("no_book") or []):
        np_, nq = _f(lvl[0] if isinstance(lvl, (list, tuple)) else None), _f(lvl[1] if isinstance(lvl, (list, tuple)) else None)
        if np_ is None or nq is None:
            continue
        yp = round(1 - np_, 4)
        if 0 < yp < 1:
            out.append((yp, nq))
    out.sort(key=lambda x: x[0])
    return out


def feasibility(rec: dict, want_contracts: float, cap: float = CAP) -> dict:
    """Walk the ask ladder: contracts fillable <= cap, their VWAP, and best-ask size."""
    ladder = yes_ask_ladder(rec)
    best_ask = _f(rec.get("yes_ask"))
    best_sz = _f(rec.get("yes_ask_size")) or 0.0
    if not ladder and best_ask is not None:
        ladder = [(best_ask, best_sz)]
    filled = cost = 0.0
    for yp, sz in ladder:
        if yp > cap:
            break
        take = min(sz, want_contracts - filled)
        if take <= 0:
            break
        filled += take
        cost += take * yp
    vwap = (cost / filled) if filled > 0 else None
    return {"best_ask": best_ask, "best_ask_size": best_sz, "fillable_contracts": round(filled, 2),
            "fill_vwap": round(vwap, 4) if vwap else None,
            "fillable_pct": round(filled / want_contracts, 3) if want_contracts > 0 else 0.0,
            "ladder_depth_usd": round(sum(yp * sz for yp, sz in ladder if yp <= cap), 2)}


def settlement_winner(event_ticker: str) -> str | None:
    try:
        mk = http_json(f"{API}/markets?event_ticker={event_ticker}&limit=100").get("markets", [])
    except Exception:
        return None
    yes = [m["ticker"] for m in mk if (m.get("settlement_value") or m.get("result")) == "yes"]
    return yes[0] if len(yes) == 1 else None


def load_state() -> dict:
    fp = OUTDIR / "paper_state.json"
    s = json.loads(fp.read_text()) if fp.exists() else {}
    for mk in ("high", "low"):
        s.setdefault(mk, {})
        s[mk].setdefault("bankroll", INIT_BANKROLL)
        s[mk].setdefault("entries", {})        # event_date -> count (the daily cap counter)
        s[mk].setdefault("picked_cities", {})  # event_date -> [city, ...] (idempotent re-fires)
    return s


def save_state(s):
    OUTDIR.mkdir(parents=True, exist_ok=True)
    (OUTDIR / "paper_state.json").write_text(json.dumps(s, indent=2))


def log_path(market):
    return OUTDIR / f"{market}_log.parquet"


def append_rows(market, rows):
    if not rows:
        return
    OUTDIR.mkdir(parents=True, exist_ok=True)
    fp = log_path(market)
    df = pd.DataFrame(rows)
    if fp.exists():
        df = pd.concat([pd.read_parquet(fp), df], ignore_index=True)
    df.to_parquet(fp, index=False)


# every raw field worth keeping ("better safe than sorry")
_RAW_KEYS = ("yes_bid", "yes_ask", "yes_bid_size", "yes_ask_size", "no_bid", "no_ask", "last",
             "vol", "vol_24h", "open_interest", "liquidity", "status")


def capture(market: str, tzname: str, event_date: str, state: dict):
    """LIVE per-(tz, market) capture: for the cities in `tzname`, fetch the live book NOW, apply the
    executable rule with the persistent daily cap, paper-record + log every scanned city."""
    cmap = HIGH if market == "high" else LOW
    hh, mm = ANCHOR_HM[market]
    bal = state[market]["bankroll"]
    ecount = state[market]["entries"].get(event_date, 0)
    picked = list(state[market]["picked_cities"].get(event_date, []))
    y, mo, d = int(event_date[:4]), int(event_date[5:7]), int(event_date[8:10])
    anchor = pd.Timestamp(y, mo, d, hh, mm, tz=ZoneInfo(tzname)).tz_convert("UTC")
    tz_cities = [(s, v) for s, v in cmap.items() if v[1] == tzname]
    if not tz_cities:
        print(f"[{market} {event_date} {tzname}] no cities in this tz"); return

    rows = []
    for series, (label, _tz, region) in tz_cities:
        et = f"{series}-{datecode(event_date)}"
        priced = {}
        for m in fetch_markets(et):
            r = rec_from_market(m)
            if r["yes_ask"] is not None:
                priced[r["ticker"]] = r
        if len(priced) < 3:
            continue
        fav = max(priced, key=lambda t: _midp(priced[t]))
        rec = priced[fav]
        fmid = _midp(rec)
        in_band = BAND[0] <= fmid <= BAND[1]
        # executable rule: enter iff in band, cap not full, city not already entered this date
        will_enter = in_band and ecount < NCITIES and label not in picked
        ranked = sorted(priced, key=lambda t: _midp(priced[t]), reverse=True)
        runner = ranked[1] if len(ranked) > 1 else None
        dist = {t: {"yes_bid": _f(r.get("yes_bid")), "yes_ask": _f(r.get("yes_ask")),
                    "last": _f(r.get("last")), "subtitle": r.get("subtitle")} for t, r in priced.items()}
        sum_ask = sum(v["yes_ask"] for v in dist.values() if v["yes_ask"] is not None)
        try:
            stale = (pd.Timestamp.now(tz="UTC") - pd.Timestamp(rec.get("ts"))).total_seconds()
        except Exception:
            stale = None
        row = {
            "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "market": market, "event_date": event_date, "tz": tzname, "anchor_local": f"{hh:02d}:{mm:02d}",
            "anchor_utc": anchor.isoformat(), "snapshot_ts": rec.get("ts"), "staleness_sec": stale,
            "series": series, "city": label, "region": region, "n_buckets": len(priced),
            "ticker": fav, "subtitle": rec.get("subtitle"), "confidence_mid": round(fmid, 4),
            "in_band": bool(in_band), "picked": bool(will_enter),
            "runnerup_ticker": runner, "runnerup_mid": round(_midp(priced[runner]), 4) if runner else None,
            "fav_minus_runnerup": round(fmid - _midp(priced[runner]), 4) if runner else None,
            "sum_yes_ask": round(sum_ask, 4),
            "entries_before": ecount, "cap": NCITIES,
            **{k: rec.get(k) for k in _RAW_KEYS},
            "yes_book": None, "no_book": None, "distribution": json.dumps(dist), "event_ticker": rec.get("event"),
            "intended_usd": None, "intended_contracts": None, "fillable_contracts": None,
            "fill_vwap": None, "fillable_pct": None, "ladder_depth_usd": None, "best_ask_size_lvl": None,
            "win": None, "paper_pnl_usd": None, "settled": False,
        }
        if will_enter:
            ob = fetch_orderbook(fav)  # already dollar-denominated {'yes': [...], 'no': [...]}
            rec["no_book"] = ob.get("no") or []
            rec["yes_book"] = ob.get("yes") or []
            ladder = yes_ask_ladder(rec)
            rec["yes_ask_size"] = ladder[0][1] if ladder else None  # size at the best live ask
            stake_each = (STAKE / NCITIES) * bal
            ask = rec["yes_ask"]
            want = stake_each / ask if ask else 0.0
            fe = feasibility(rec, want)
            ecount += 1
            picked.append(label)
            row.update(yes_book=json.dumps(rec["yes_book"]), no_book=json.dumps(rec["no_book"]),
                       intended_usd=round(stake_each, 2), intended_contracts=round(want, 2),
                       fillable_contracts=fe["fillable_contracts"], fill_vwap=fe["fill_vwap"],
                       fillable_pct=fe["fillable_pct"], ladder_depth_usd=fe["ladder_depth_usd"],
                       best_ask_size_lvl=fe["best_ask_size"], yes_ask_size=rec["yes_ask_size"])
        rows.append(row)
        time.sleep(0.25)  # gentle stagger across the tz's cities (keyless tier is shared)

    state[market]["entries"][event_date] = ecount
    state[market]["picked_cities"][event_date] = picked
    if not rows:
        print(f"[{market} {event_date} {tzname}] no tradeable books at the anchor"); return
    append_rows(market, rows)
    nip = sum(r["in_band"] for r in rows)
    nnew = sum(r["picked"] for r in rows)
    print(f"[{market} {event_date} {tzname}] {len(rows)} cities | {nip} in-band | {nnew} entered "
          f"(cap {ecount}/{NCITIES}, bankroll ${bal:.2f}, ${(STAKE/NCITIES)*bal:.2f}/slot):")
    for r in rows:
        if r["picked"]:
            print(f"   ENTER {r['city']:<14} {r['ticker']:<24} conf={r['confidence_mid']:.3f} "
                  f"ask={r['yes_ask']} fillable={r['fillable_pct']:.0%}@vwap{r['fill_vwap']} "
                  f"depth=${r['ladder_depth_usd']:.0f}")


def settle(market: str, state: dict):
    fp = log_path(market)
    if not fp.exists():
        print(f"[{market}] no log yet"); return
    df = pd.read_parquet(fp)
    openrows = df[df["settled"] == False]  # noqa: E712
    if openrows.empty:
        print(f"[{market}] nothing open to settle"); return
    cache: dict[str, str | None] = {}
    booked = 0
    for idx, r in openrows.iterrows():
        ev = "-".join(str(r["ticker"]).split("-")[:2])
        if ev not in cache:
            cache[ev] = settlement_winner(ev)
        win_ticker = cache[ev]
        if win_ticker is None:
            continue  # event not settled yet
        win = (win_ticker == r["ticker"])
        df.at[idx, "win"] = bool(win)
        df.at[idx, "settled"] = True
        if r["picked"]:  # paper P&L + bankroll only on the cities we actually "traded"
            fill = r["fill_vwap"] if pd.notna(r["fill_vwap"]) else r["yes_ask"]
            c = r["fillable_contracts"] if (pd.notna(r["fillable_contracts"]) and r["fillable_contracts"]) \
                else (r["intended_usd"] / fill if fill else 0.0)
            cost = c * fill + FEE(fill) * c
            pnl = (c - cost) if win else (-cost)
            df.at[idx, "paper_pnl_usd"] = round(float(pnl), 2)
            state[market]["bankroll"] += float(pnl)
        booked += 1
    df.to_parquet(fp, index=False)
    sp = df[(df["settled"] == True) & (df["picked"] == True)]  # noqa: E712
    hit = sp["win"].mean() if len(sp) else float("nan")
    fillm = sp["fillable_pct"].mean() if len(sp) else float("nan")
    print(f"[{market}] settled {booked} rows | bankroll ${state[market]['bankroll']:.2f} | picks all-time: "
          f"n={len(sp)} hit={hit:.1%} pnl=${sp['paper_pnl_usd'].sum():.2f} avg_fillable={fillm:.0%}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["high", "low", "both"], required=True)
    ap.add_argument("--tz", default=None, help="IANA tz; capture only cities in this tz (LIVE). Required for --capture.")
    ap.add_argument("--capture", action="store_true")
    ap.add_argument("--settle", action="store_true")
    ap.add_argument("--event-date", default=None, help="YYYY-MM-DD (default: today's local date in --tz)")
    args = ap.parse_args(argv)
    if not (args.capture or args.settle):
        args.capture = args.settle = True
    state = load_state()
    markets = ["high", "low"] if args.market == "both" else [args.market]
    print(f"directional_paper (live per-anchor) | now_utc={datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    for mk in markets:
        if args.settle:
            settle(mk, state)
        if args.capture:
            if not args.tz:
                print(f"[{mk}] --capture needs --tz"); continue
            ed = args.event_date or datetime.now(ZoneInfo(args.tz)).date().isoformat()
            capture(mk, args.tz, ed, state)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
