"""Verify each paper.yaml city's configured settlement station against Kalshi's
OFFICIAL settlement source -- the market's `rules_primary` text, which names the
exact NWS site (airport + call sign) the daily high settles on.

Keyless public read of /markets. For each series it grabs a current (or most
recent) market, pulls rules_primary, extracts the station call sign / airport
name Kalshi actually settles on, and diffs it against config/paper.yaml.

Run:  python scripts/verify_stations.py
"""
from __future__ import annotations

import re
import sys
import time
from pathlib import Path

import httpx
import yaml

API = "https://api.elections.kalshi.com/trade-api/v2"
CFG = Path("config/paper.yaml")

# call sign -> lowercase name fragments that identify it in free-text rules
AIRPORTS = {
    "KMDW": ["midway"],
    "KORD": ["o'hare", "ohare", "o hare"],
    "KHOU": ["hobby"],
    "KIAH": ["bush", "intercontinental"],
    "KNYC": ["central park"],
    "KLGA": ["laguardia", "la guardia"],
    "KJFK": ["kennedy", "jfk"],
    "KMIA": ["miami international"],
    "KAUS": ["bergstrom", "austin-bergstrom", "austin bergstrom"],
    "KATT": ["mabry", "camp mabry"],
    "KDEN": ["denver international"],
    "KPHL": ["philadelphia international"],
    "KLAX": ["los angeles international"],
    "KLAS": ["mccarran", "harry reid", "las vegas"],
    "KMSY": ["louis armstrong", "new orleans international"],
    "KSEA": ["seattle-tacoma", "sea-tac", "seatac", "seattle tacoma"],
    "KSFO": ["san francisco international"],
    "KDCA": ["reagan", "national airport"],
    "KIAD": ["dulles"],
    "KATL": ["hartsfield", "atlanta international"],
    "KMSP": ["minneapolis", "st. paul", "st paul"],
    "KPHX": ["sky harbor", "phoenix"],
    "KBOS": ["logan", "boston"],
    "KDFW": ["dfw", "dallas-fort worth", "dallas/fort worth", "dallas fort worth"],
    "KDAL": ["love field", "dallas love"],
    "KOKC": ["will rogers", "oklahoma city"],
    "KSAT": ["san antonio international"],
}


def load_markets() -> list[tuple[str, str, str]]:
    cfg = yaml.safe_load(CFG.read_text())
    return [(m["name"], m["event_pattern"], m["station"]) for m in cfg["markets"]]


def fetch_rules(client: httpx.Client, series: str) -> tuple[str, str, str, str]:
    """Return (market_ticker, status, rules_primary, rules_secondary) for a current/recent market."""
    for status in ("open", "unopened", "settled", None):
        params: dict = {"series_ticker": series, "limit": 1}
        if status:
            params["status"] = status
        try:
            r = client.get("/markets", params=params)
        except httpx.HTTPError as e:
            print(f"  ! {series} {status}: {e}", file=sys.stderr)
            time.sleep(0.5)
            continue
        if r.status_code != 200:
            print(f"  ! {series} {status}: HTTP {r.status_code} {r.text[:120]}", file=sys.stderr)
            time.sleep(0.5)
            continue
        markets = r.json().get("markets", [])
        if not markets:
            time.sleep(0.3)
            continue
        m = markets[0]
        rp = m.get("rules_primary") or ""
        rs = m.get("rules_secondary") or ""
        tkr = m.get("ticker", "")
        if (not rp or not rs) and tkr:  # list omitted rules -> pull the single market
            r2 = client.get(f"/markets/{tkr}")
            if r2.status_code == 200:
                full = (r2.json().get("market", {}) or {})
                rp = rp or (full.get("rules_primary", "") or "")
                rs = rs or (full.get("rules_secondary", "") or "")
        return tkr, (m.get("status") or status or "?"), rp, rs
    return "", "NO-MARKET", "", ""


def station_from_rules(rp: str, rs: str) -> tuple[str, list[str], str]:
    """Return (authoritative_station, corroborating_signals, location_snippet).

    AUTHORITATIVE: the NWS Climatological Report code `CLI<xxx>` (in rules_primary
    or _secondary) IS Kalshi's settlement station -> the call sign is 'K'+xxx
    (e.g. CLIDFW -> KDFW, CLIHOU -> Houston-Hobby -> KHOU, CLIMDW -> KMDW).
    Corroboration: explicit call signs + recognized airport names.
    """
    text = (rp or "") + "\n" + (rs or "")
    low = text.lower()

    cli = re.search(r"\bCLI([A-Z]{3})\b", text)
    cli_station = "K" + cli.group(1) if cli else ""

    found: list[str] = []
    for c in re.findall(r"\bK[A-Z]{3}\b", text):  # explicit call signs
        if c not in found:
            found.append(c)
    for call, frags in AIRPORTS.items():           # airport-name recognition
        if any(f in low for f in frags) and call not in found:
            found.append(call)

    # human-readable location: NWS rules say  choosing the location "Houston-Hobby, TX"
    snippet = ""
    mloc = re.search(r'location\s+"([^"]+)"', text)
    if mloc:
        snippet = mloc.group(1)
    else:
        for kw in ("maximum temperature", "high temperature", "recorded at", "recorded in"):
            i = low.find(kw)
            if i >= 0:
                snippet = re.sub(r"\s+", " ", text[max(0, i - 10): i + 90]).strip()
                break
    return cli_station, found, snippet


def main() -> int:
    rows = load_markets()
    headers = {"User-Agent": "weather-alpha-verify/0.1"}
    results = []
    with httpx.Client(base_url=API, timeout=20.0, headers=headers) as c:
        for name, series, cfg_station in rows:
            tkr, status, rp, rs = fetch_rules(c, series)
            cli_station, found, snippet = station_from_rules(rp, rs)
            # AUTHORITATIVE: the CLI code. Fall back to name/call-sign corroboration only
            # when no CLI code is present (older series sometimes omit it).
            official = cli_station or (found[0] if found else "")
            if not rp and not rs:
                verdict = "NO-RULES"
            elif cli_station:
                verdict = "MATCH" if cfg_station == cli_station else "MISMATCH"
            elif cfg_station in found:
                verdict = "MATCH(name)"
            elif found:
                verdict = "MISMATCH"
            else:
                verdict = "UNRESOLVED"
            results.append((name, series, cfg_station, official, cli_station, verdict, snippet))
            print(f"[{verdict:11}] {name:5} {series:14} cfg={cfg_station:5} "
                  f"official={official or '-':5} (CLI={cli_station or '-'})  loc={snippet or '-'}")
            time.sleep(0.4)  # gentle on the public rate limit

    print("\n" + "=" * 86)
    print("SUMMARY -- config vs OFFICIAL Kalshi settlement source (NWS CLI code in rules)")
    print("=" * 86)
    print(f"{'CITY':5} {'SERIES':14} {'CFG':5} {'OFFICIAL':9} {'CLI-CODE':9} {'VERDICT':12} LOCATION")
    print("-" * 86)
    bad = 0
    for name, series, cfg_station, official, cli_station, verdict, snippet in results:
        if not verdict.startswith("MATCH"):
            bad += 1
        cli_disp = ("CLI" + cli_station[1:]) if cli_station else "-"
        print(f"{name:5} {series:14} {cfg_station:5} {official or '-':9} {cli_disp:9} {verdict:12} {snippet or '-'}")
    print("-" * 86)
    print(f"{len(results) - bad}/{len(results)} MATCH; {bad} need attention")
    return 0


if __name__ == "__main__":
    sys.exit(main())
