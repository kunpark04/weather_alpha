"""Run market_accuracy across ALL Kalshi daily temperature markets (HIGH + LOW, every
city) at intraday anchor hours 8AM-1PM local, under ONE shared rate-limit.

Catalog is from the 2026-05-31 discovery pass (verified live). The driver pre-checks
each series (1 recent settled event) and skips dead tickers rather than wasting the long
candlestick crawl on them.

Output:
  - data/accuracy_all.parquet           per-(series,day,hour) rows for all markets
  - printed Table A: Top-3 accuracy by anchor hour, per market (the anchor question)
  - printed Table B: 1PM tradeability per market (Top-1/Top-3/wing coverage/affordable)

print_summary() reads the saved parquet shape, so scripts/summarize_accuracy.py can
re-render the tables without re-crawling. Public API only (no auth). ~30-60 min run.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from market_accuracy import API, analyze_series, _get  # noqa: E402

HOURS = [8, 9, 10, 11, 12, 13]
DAYS_BACK = 75
CONCURRENCY = 4

# (series_ticker, city, kind, IANA tz) — verified live 2026-05-31 (see discovery pass).
CATALOG = [
    # ---- daily HIGH ----
    ("KXHIGHCHI",   "Chicago",      "high", "America/Chicago"),
    ("KXHIGHNY",    "New York",     "high", "America/New_York"),
    ("KXHIGHLAX",   "Los Angeles",  "high", "America/Los_Angeles"),
    ("KXHIGHMIA",   "Miami",        "high", "America/New_York"),
    ("KXHIGHTHOU",  "Houston",      "high", "America/Chicago"),
    ("KXHIGHTPHX",  "Phoenix",      "high", "America/Phoenix"),     # no DST
    ("KXHIGHTSFO",  "San Francisco","high", "America/Los_Angeles"),
    ("KXHIGHTLV",   "Las Vegas",    "high", "America/Los_Angeles"),
    ("KXHIGHTNOLA", "New Orleans",  "high", "America/Chicago"),
    ("KXHIGHTSATX", "San Antonio",  "high", "America/Chicago"),
    ("HIGHAUS",     "Austin",       "high", "America/Chicago"),     # DEAD 05-31 (ticker TBD)
    ("KXHIGHTDEN",  "Denver",       "high", "America/Denver"),      # DEAD 05-31 (ticker TBD)
    # ---- daily LOW ----
    ("KXLOWTNYC",   "New York",     "low",  "America/New_York"),
    ("KXLOWTDEN",   "Denver",       "low",  "America/Denver"),
    ("KXLOWTBOS",   "Boston",       "low",  "America/New_York"),
    ("KXLOWTPHX",   "Phoenix",      "low",  "America/Phoenix"),     # no DST
    ("KXLOWTDAL",   "Dallas",       "low",  "America/Chicago"),
    ("KXLOWTATL",   "Atlanta",      "low",  "America/New_York"),
    ("KXLOWTOKC",   "Oklahoma City","low",  "America/Chicago"),
    ("KXLOWTLAX",   "Los Angeles",  "low",  "America/Los_Angeles"),
    ("KXLOWTMIA",   "Miami",        "low",  "America/New_York"),
    ("KXLOWTNOLA",  "New Orleans",  "low",  "America/Chicago"),
    ("KXLOWTSATX",  "San Antonio",  "low",  "America/Chicago"),
    ("KXLOWAUS",    "Austin",       "low",  "America/Chicago"),     # DEAD 05-31 (ticker TBD)
]


async def _series_live(client, sem, series, tz) -> bool:
    """Cheap pre-check: is there a settled 6-bucket event in the last few days?"""
    today = pd.Timestamp.now(tz=tz).normalize().tz_localize(None)
    for k in range(1, 6):
        d = today - pd.Timedelta(days=k)
        ev = f"{series}-{d.strftime('%y%b%d').upper()}"
        ok, mk = await _get(client, sem, "/markets", {"event_ticker": ev, "limit": 100})
        if ok and len(mk.get("markets") or []) >= 5:
            return True
    return False


def print_summary(alldf: pd.DataFrame, hours: list[int]) -> None:
    """Render Table A (Top-3 by anchor hour) + Table B (1PM tradeability) from the
    per-(series,day,hour) frame. Pure / re-runnable from the saved parquet."""
    def agg(g, h, col):
        gh = g[g["hour"] == h]
        return gh[col].mean() if not gh.empty else float("nan")

    rows = []
    for (s, city, kind), g in alldf.groupby(["series", "city", "kind"], sort=False):
        row = {"market": f"{city} {kind}", "series": s, "kind": kind, "N": g["date"].nunique()}
        for h in hours:
            row[f"T3@{h}"] = agg(g, h, "top3")
        g13 = g[g["hour"] == 13]
        aff = g13["wing_sum_asks"] < 0.90
        row["T1@13"] = agg(g, 13, "top1")
        row["cov@13"] = g13.loc[aff, "wing_covers"].mean() if aff.any() else float("nan")
        row["aff@13"] = aff.mean() if not g13.empty else float("nan")
        rows.append(row)
    summ = pd.DataFrame(rows).sort_values(["kind", "market"])

    def pct(x):
        return "  -- " if x != x else f"{x:5.0%}"

    print("\n" + "=" * 86)
    print("TABLE A - Top-3 accuracy (winner within market favorite +/-1) by ANCHOR HOUR (local)")
    print("=" * 86)
    print(f"{'market':<20}{'N':>4}  " + "".join(f"{str(h) + ':00':>7}" for h in hours))
    for _, r in summ.iterrows():
        print(f"{r['market']:<20}{int(r['N']):>4}  " + "".join(pct(r[f'T3@{h}']) + " " for h in hours))

    print("\n" + "=" * 86)
    print("TABLE B - 1PM-anchor tradeability (does market_wing extend?)")
    print("=" * 86)
    print(f"{'market':<20}{'N':>4}{'Top1':>7}{'Top3':>7}{'wing_cov':>10}{'affordable':>12}")
    for _, r in summ.iterrows():
        print(f"{r['market']:<20}{int(r['N']):>4}{pct(r['T1@13']):>7}{pct(r['T3@13']):>7}"
              f"{pct(r['cov@13']):>10}{pct(r['aff@13']):>12}")
    print("\nwing_cov = of affordable 1PM days, % winner inside the 2-leg wing.  "
          "affordable = % of 1PM days the wing costs < $0.90.")


def print_by_anchor(alldf: pd.DataFrame, hours: list[int], csv_out="data/accuracy_by_anchor.csv") -> None:
    """For EACH anchor hour, a per-city table of Top-1/2/3 + wing economics.

    T1 = favorite (modal) bucket correct.
    T2 = winner inside the 2-leg drop_lower_ask wing (modal + the higher-ask adjacent) —
         the strategy's actual coverage; ~= 'winner in the 2 likeliest buckets'.
    T3 = winner within modal +/-1 (the full 3-leg wing).  T1 <= T2 <= T3 by construction.
    aff% = share of days the 2-leg wing costs < $0.90 (i.e. the strat would fire).
    cost/cov/EV are over AFFORDABLE days only: cost = avg wing sum_asks, cov = T2 on those
    days. EV? = yes iff cov > cost (the equal-payout wing breaks even ex-fees exactly there).
    """
    def p(x):
        return "  -- " if x != x else f"{x:4.0%}"

    recs = []
    for h in hours:
        gh = alldf[alldf["hour"] == h]
        print(f"\n================== ANCHOR {h:02d}:00 local ==================")
        print(f"{'city / kind':<20}{'N':>4}{'T1':>6}{'T2':>6}{'T3':>6}{'aff%':>6}"
              f"{'nAff':>5}{'cost':>6}{'cov':>6}{'EV?':>5}")
        for (city, kind), g in sorted(gh.groupby(["city", "kind"]), key=lambda kv: (kv[0][1], kv[0][0])):
            n = len(g)
            t1, t2, t3 = g["top1"].mean(), g["wing_covers"].mean(), g["top3"].mean()
            aff = g["wing_sum_asks"] < 0.90
            naff = int(aff.sum())
            cost = g.loc[aff, "wing_sum_asks"].mean() if naff else float("nan")
            cov = g.loc[aff, "wing_covers"].mean() if naff else float("nan")
            ev = ("yes" if cov > cost else "no") if naff else "--"
            flag = "*" if 0 < naff < 10 else ""
            costs = "  -- " if cost != cost else f"{cost:4.2f}"
            print(f"{city + ' ' + kind:<20}{n:>4}{p(t1):>6}{p(t2):>6}{p(t3):>6}{p(aff.mean()):>6}"
                  f"{naff:>5}{costs:>6}{p(cov):>6}{ev:>4}{flag}")
            recs.append({"hour": h, "city": city, "kind": kind, "N": n, "top1": t1,
                         "top2_wing": t2, "top3": t3, "aff_pct": aff.mean(), "n_aff": naff,
                         "cost": cost, "cov_aff": cov, "ev_positive": (cov > cost) if naff else None})
    pd.DataFrame(recs).to_csv(csv_out, index=False)
    print(f"\n* = <10 affordable days (cost/cov/EV unreliable).   full grid -> {csv_out}")


async def main():
    sem = asyncio.Semaphore(CONCURRENCY)
    frames = []
    async with httpx.AsyncClient(base_url=API, timeout=25,
                                 headers={"User-Agent": "weather-alpha-accuracy/0.1"}) as client:
        print("Pre-checking which catalog tickers are live...")
        live = []
        for series, city, kind, tz in CATALOG:
            ok = await _series_live(client, sem, series, tz)
            print(f"  {'LIVE ' if ok else 'DEAD '} {series:<14} {city} {kind}")
            if ok:
                live.append((series, city, kind, tz))
        print(f"\n{len(live)}/{len(CATALOG)} series live. Crawling candlesticks "
              f"(hours {HOURS}, last {DAYS_BACK}d, concurrency {CONCURRENCY})...\n")

        for i, (series, city, kind, tz) in enumerate(live, 1):
            df, n_fail = await analyze_series(client, sem, series, tz, HOURS, DAYS_BACK)
            nd = df["date"].nunique() if not df.empty else 0
            print(f"  [{i}/{len(live)}] {series:<14} {city:<13} {kind:<4} {nd:>3} days  {n_fail:>2} fail")
            if not df.empty:
                df["city"], df["kind"], df["tz"] = city, kind, tz
                frames.append(df)

    if not frames:
        print("\nNo data collected.")
        return
    alldf = pd.concat(frames, ignore_index=True)
    out = "data/accuracy_all.parquet"
    alldf.to_parquet(out)
    print_summary(alldf, HOURS)
    print(f"\nfull per-(series,day,hour) rows -> {out}")


if __name__ == "__main__":
    asyncio.run(main())
