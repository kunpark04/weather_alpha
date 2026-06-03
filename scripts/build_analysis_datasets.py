"""Build the organized, self-documenting analysis datasets under data/analysis/.

Produces two datasets, each as a self-describing JSON (embedded `_meta` spec block + `data`
array) AND a flat CSV (with a `#`-commented spec header that pandas skips via comment='#'):

  coverage_by_anchor.{json,csv}  -- cumulative top-1/2/3 settlement hit-rate by MARKET rank,
                                    per city per LOCAL anchor hour (6 AM..4 PM), + pooled.
  edge_by_anchor.{json,csv}      -- break-even economics of an equal-payout top-2 coverage,
                                    per city per local anchor hour, UNCONDITIONAL + GATED
                                    (max_ask_sum<=0.90), with fire rate, + pooled.

Plus README.md indexing both. Re-run after a fresh backfill to regenerate. Deterministic.
"""
from __future__ import annotations

import json
import statistics as st
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from scripts.modal_rank_multicity import CITY, load_city, BACKFILL

_ROOT = Path(__file__).resolve().parent.parent
OUT = _ROOT / "data" / "analysis"
HOURS = list(range(6, 17))          # 6 AM .. 4 PM, each in the city's own local tz
GATE = 0.90
FEE_RATE = 0.07


def _fee(p: float) -> float:
    return FEE_RATE * p * (1.0 - p)   # dollars/contract/leg, at scale (ceil rounding ignored)


def _ask(last_cents: float) -> float:
    return min(last_cents / 100.0 + 0.01, 0.99)   # last-trade + 1 tick proxy (see _meta)


def city_per_hour(series: str) -> dict[int, list[dict]] | None:
    """Per anchor hour -> list of per-day primitives {t1,t2,t3,cost,win_top2,fee}."""
    df = load_city(series)
    if df.empty:
        return None
    tz = ZoneInfo(CITY[series][1])
    ph: dict[int, list[dict]] = {h: [] for h in HOURS}
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1:
            continue
        win = w[0]
        day = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
        for h in HOURS:
            anchor = pd.Timestamp(ev.year, ev.month, ev.day, h, tz=tz).tz_convert("UTC")
            last = day[day["timestamp"] <= anchor].groupby("ticker")["yes_price_cents"].last()
            if last.size < 3:
                continue
            ranked = list(last.sort_values(ascending=False).index)
            top2 = ranked[:2]
            asks = [_ask(last[t]) for t in top2]
            ph[h].append({
                "t1": win == ranked[0], "t2": win in ranked[:2], "t3": win in ranked[:3],
                "cost": sum(asks), "win_top2": win in set(top2), "fee": sum(_fee(a) for a in asks),
            })
    return ph


def _r(x, n=4):
    return round(float(x), n)


def _cov_row(scope, city, series, tz, h, days):
    return {"scope": scope, "city": city, "series": series, "tz": tz,
            "anchor_local_hour": h, "n_days": len(days),
            "top1": _r(st.mean(d["t1"] for d in days)),
            "top2": _r(st.mean(d["t2"] for d in days)),
            "top3": _r(st.mean(d["t3"] for d in days))}


def _edge_row(scope, city, series, h, gate, subset, n_total):
    cost = st.mean(d["cost"] for d in subset)
    fee = st.mean(d["fee"] for d in subset)
    wr = st.mean(1.0 if d["win_top2"] else 0.0 for d in subset)
    return {"scope": scope, "city": city, "series": series, "anchor_local_hour": h,
            "gate": gate, "n_days": len(subset), "n_total": n_total,
            "fire_rate": _r(len(subset) / n_total), "win_rate": _r(wr),
            "mean_cost": _r(cost), "mean_fee": _r(fee),
            "breakeven": _r(cost + fee), "edge": _r(wr - (cost + fee))}


def build():
    cov, edge = [], []
    pooled = {h: [] for h in HOURS}
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        ph = city_per_hour(s)
        if not ph:
            continue
        city, tz = CITY[s]
        for h in HOURS:
            days = ph[h]
            if not days:
                continue
            cov.append(_cov_row("city", city, s, tz, h, days))
            edge.append(_edge_row("city", city, s, h, "all", days, len(days)))
            gated = [d for d in days if d["cost"] <= GATE]
            if gated:
                edge.append(_edge_row("city", city, s, h, "gated_le_0.90", gated, len(days)))
            pooled[h] += days
    for h in HOURS:
        days = pooled[h]
        if not days:
            continue
        cov.append(_cov_row("pooled", "ALL", None, None, h, days))
        edge.append(_edge_row("pooled", "ALL", None, h, "all", days, len(days)))
        gated = [d for d in days if d["cost"] <= GATE]
        if gated:
            edge.append(_edge_row("pooled", "ALL", None, h, "gated_le_0.90", gated, len(days)))
    return cov, edge


COV_META = {
    "dataset": "coverage_by_anchor",
    "description": "Cumulative top-1/2/3 settlement hit-rate by MARKET RANK for 20 Kalshi "
                   "daily-high-temperature city markets, per city per LOCAL anchor hour (6 AM-4 PM).",
    "each_row_is": "one (city OR pooled-across-cities) x anchor_local_hour observation",
    "source_data": "Kalshi public trade history (GET /markets/trades) pulled by "
                   "scripts/backfill_cities.py into data/backfill/<SERIES>/<YYYY-MM-DD>.zip",
    "coverage_window": "settled event-days ~2026-03-27..2026-06-01 (Kalshi temp markets have NO "
                       "tradeable market data before ~late Mar 2026); N~=66 days per city.",
    "method": [
        "anchor_local_hour h is each city's OWN local wall-clock hour (IANA tz, DST-correct); "
        "the per-bucket snapshot is the last trade at/before h:00 local, converted via UTC.",
        "market rank: buckets ordered by yes price (last trade, cents) descending; rank-1 = market modal.",
        "truth: the settled bucket is the market with settlement_value=='yes' (Kalshi's own settlement; "
        "no external CLI/station data, so no station-mapping or basis risk).",
        "topK = fraction of days where the settled bucket is among the K highest-priced buckets at the anchor.",
        "a day is skipped at an hour if fewer than 3 buckets have a trade at/before that hour.",
    ],
    "columns": {
        "scope": "'city' or 'pooled' (pooled = all cities' days combined at that hour)",
        "city": "city label ('ALL' for pooled)",
        "series": "Kalshi series ticker (null for pooled)",
        "tz": "IANA timezone of the city's anchor (null for pooled)",
        "anchor_local_hour": "hour of day in the city's local tz, 6..16",
        "n_days": "number of city-days contributing",
        "top1": "P(settled bucket == highest-priced bucket)",
        "top2": "P(settled bucket in the 2 highest-priced buckets)",
        "top3": "P(settled bucket in the 3 highest-priced buckets)",
    },
    "caveats": ["single season (spring 2026)", "last-trade price proxy (not depth)",
                "~66 days/city -> treat <20-day cells cautiously"],
}

EDGE_META = {
    "dataset": "edge_by_anchor",
    "description": "Break-even economics of an equal-payout TOP-2 COVERAGE trade for 20 Kalshi "
                   "daily-high-temp city markets, per city per local anchor hour, UNCONDITIONAL and "
                   "under the production max_ask_sum<=0.90 gate.",
    "each_row_is": "one (city OR pooled) x anchor_local_hour x gate-scope observation",
    "trade_model": [
        "coverage = buy the two highest-priced buckets with equal payout: pay (a1+a2), receive $1 if either wins.",
        "profit_per_day = win_top2 - (a1+a2) - fees ;  break-even win rate = (a1+a2) + fees ;  edge = win_rate - breakeven.",
    ],
    "ask_proxy": "a_i = min(last_trade_price_i/100 + 0.01, 0.99).  The backfill stores executed TRADES, "
                 "not the live order book, so the ASK (the price you'd PAY to buy) is estimated as the last "
                 "trade + 1 tick (1c, Kalshi's minimum increment). This is the TIGHTEST plausible ask; real "
                 "spreads are usually wider, so mean_cost/breakeven are LOWER bounds and edge is an "
                 "OPTIMISTIC upper bound. (Going forward the orderbook logger captures the real book, "
                 "removing the need for this proxy.)",
    "fee_model": "Kalshi fee per leg = 0.07 * p * (1-p) dollars/contract, summed over both legs (at scale; "
                 "the per-trade ceil-to-cent rounding is ignored, so small-clip fees are understated).",
    "gate": "gate='all' = every day; gate='gated_le_0.90' keeps only days where cost (a1+a2) <= 0.90 "
            "(the production max_ask_sum). fire_rate = n_days(gated)/n_total(all days that hour).",
    "columns": {
        "scope": "'city' or 'pooled'", "city": "city label ('ALL' for pooled)",
        "series": "Kalshi series ticker (null for pooled)",
        "anchor_local_hour": "hour in the city's local tz, 6..16",
        "gate": "'all' or 'gated_le_0.90'",
        "n_days": "days in this (hour, gate) cell", "n_total": "all days at this hour (gate denominator)",
        "fire_rate": "n_days / n_total (1.0 for gate='all')",
        "win_rate": "P(settled bucket in the top-2) on these days",
        "mean_cost": "mean (a1+a2) = mean cost to cover the top-2 (ask proxy)",
        "mean_fee": "mean two-leg fee in dollars",
        "breakeven": "mean_cost + mean_fee = win rate needed to break even",
        "edge": "win_rate - breakeven (>0 = profitable; OPTIMISTIC, see ask_proxy)",
    },
    "caveats": ["in-sample: the gate is measured on the same days it selects (no OOS split)",
                "top-2-BY-PRICE, not the production positional wing (modal+adjacents) or drop_lower_ask",
                "ask proxy makes edge an optimistic upper bound", "single season (spring 2026)",
                "cells with n_days<20 are not conclusive (project rule #4)"],
}


def _write(name, meta, rows):
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps({"_meta": meta, "data": rows}, indent=2), encoding="utf-8")
    df = pd.DataFrame(rows)
    hdr = [f"# {meta['dataset']} -- {meta['description']}",
           "# Self-documenting: full spec (method, ask_proxy, fee_model, caveats) in the matching .json _meta.",
           "# Columns: " + "; ".join(f"{k}={v}" for k, v in meta["columns"].items()),
           "# Lines starting with # are comments (pandas: read_csv(path, comment='#'))."]
    with open(OUT / f"{name}.csv", "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(hdr) + "\n")
        df.to_csv(f, index=False)
    return len(rows)


def main():
    cov, edge = build()
    n1 = _write("coverage_by_anchor", COV_META, cov)
    n2 = _write("edge_by_anchor", EDGE_META, edge)
    zips = len(list(BACKFILL.glob("*/*.zip")))
    readme = f"""# data/analysis -- derived analysis datasets

Self-documenting outputs of `scripts/build_analysis_datasets.py` (re-run to regenerate).
Source: {zips} per-city/day trade zips in `../backfill/` pulled by `scripts/backfill_cities.py`
(Kalshi `/markets/trades`, ~2026-03-27..2026-06-01, ~66 days x 20 cities).

| File | Rows | What it is |
|---|---|---|
| `coverage_by_anchor.json` / `.csv` | {n1} | top-1/2/3 settlement hit-rate by market rank, per city per local anchor hour (6 AM-4 PM) + pooled |
| `edge_by_anchor.json` / `.csv` | {n2} | top-2 coverage break-even economics (cost/fee/breakeven/edge), unconditional + max_ask_sum<=0.90 gated, per city per anchor hour + pooled |

Each `.json` carries a full `_meta` spec (method, ask_proxy, fee_model, gate, columns, caveats);
each `.csv` mirrors the data with a `#`-commented header (read with `pandas.read_csv(p, comment='#')`).

Key methodology notes (see `_meta` for the authoritative version):
- Anchors are each city's OWN local hour (DST-correct). Truth = Kalshi `settlement_value=='yes'`.
- `edge_by_anchor` uses the **last-trade + 1c ask proxy** for cost, so edges are an **optimistic upper bound**.
- Analyses are in-sample, single-season (spring 2026); cells with `n_days<20` are suggestive only.
"""
    (OUT / "README.md").write_text(readme, encoding="utf-8")
    print(f"wrote data/analysis/: coverage rows={n1}, edge rows={n2}, README.md (from {zips} zips)")


if __name__ == "__main__":
    main()
