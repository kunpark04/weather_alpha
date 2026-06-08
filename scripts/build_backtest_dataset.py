"""Build data/analysis/backtest_by_city.{json,csv}: the PRODUCTION market_wing backtest per city
(flat-$ and quarter-Kelly), joined with the prior top-2 edge signal (edge_by_anchor, 1 PM gated),
whose HEADLINE metric is the % of previously positive-edge cities that were actually PROFITABLE in
each backtest.

Same self-documenting format as build_analysis_datasets.py (embedded `_meta` spec + `data` array
JSON, plus a #-commented-header CSV). Also prints the flat + Kelly tables to the terminal.

"Positive-edge city" = edge_by_anchor edge>0 at the 1 PM anchor under the production
max_ask_sum<=0.90 gate. NOTE the edge signal is a TOP-2-BY-PRICE coverage (a1+a2 vs win_top2),
while the backtest is the POSITIONAL WING (modal+adjacents) with drop_lower_ask -- same anchor +
gate, DIFFERENT trade structure, so this is a cross-check of the proxy, not a tautology.
"""
from __future__ import annotations

import json
import statistics as stat
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import metrics, run_one
from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from weather_alpha.config import load_config

OUT = _ROOT / "data" / "analysis"
EDGE_HOUR = 13                       # production 1 PM local anchor
GATE = "gated_le_0.90"               # production max_ask_sum<=0.90


def _edge_at_1pm() -> tuple[dict[str, float], dict[str, float]]:
    """series -> edge at the 1 PM anchor, for (gated_le_0.90, all)."""
    e = pd.read_csv(OUT / "edge_by_anchor.csv", comment="#")
    c = e[(e.scope == "city") & (e.anchor_local_hour == EDGE_HOUR)]
    gated = c[c.gate == GATE].set_index("series")["edge"].to_dict()
    uncond = c[c.gate == "all"].set_index("series")["edge"].to_dict()
    return gated, uncond


def build() -> list[dict]:
    cfg = load_config()
    gated, uncond = _edge_at_1pm()
    rows = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        mf = metrics(run_one(df, s, cfg, sizing="flat", drop_lower=True, drop_higher=False))
        mk = metrics(run_one(df, s, cfg, sizing="kelly", drop_lower=True, drop_higher=False))
        if not mf or not mk:
            continue
        eg = gated.get(s)
        rows.append({
            "city": CITY[s][0], "series": s,
            "edge_1pm_gated": None if eg is None else round(float(eg), 4),
            "edge_1pm_uncond": None if uncond.get(s) is None else round(float(uncond[s]), 4),
            "positive_edge": bool(eg is not None and eg > 0),
            "days": mf["days"], "n_pos": mf["pos"],
            "flat_wr": round(mf["wr"], 4), "flat_pnl": round(mf["pnl"], 2),
            "flat_sharpe": round(mf["sharpe"], 2), "flat_dd_usd": round(mf["dd_usd"], 2),
            "flat_profitable": bool(mf["pnl"] > 0),
            "kelly_pnl": round(mk["pnl"], 2), "kelly_final": round(mk["final"], 2),
            "kelly_sharpe": round(mk["sharpe"], 2), "kelly_dd_pct": round(mk["dd_pct"], 2),
            "kelly_profitable": bool(mk["pnl"] > 0),
        })
    return rows


def _hit(rows, mask_key, prof_key) -> dict:
    sub = [r for r in rows if r[mask_key]]
    prof = [r for r in sub if r[prof_key]]
    return {"n": len(sub), "profitable": len(prof),
            "pct": round(len(prof) / len(sub), 4) if sub else None,
            "cities": [r["city"] for r in sub]}


def headline(rows) -> dict:
    pe_flat = _hit(rows, "positive_edge", "flat_profitable")
    pe_kelly = _hit(rows, "positive_edge", "kelly_profitable")
    ne_flat = _hit([{**r, "neg": not r["positive_edge"]} for r in rows], "neg", "flat_profitable")
    ne_kelly = _hit([{**r, "neg": not r["positive_edge"]} for r in rows], "neg", "kelly_profitable")
    pe = [r for r in rows if r["positive_edge"]]
    return {
        "positive_edge_definition": "edge_by_anchor.edge > 0 at anchor_local_hour=13 (1 PM), "
                                    "gate=gated_le_0.90 (production max_ask_sum<=0.90)",
        "n_positive_edge_cities": pe_flat["n"],
        "positive_edge_cities": pe_flat["cities"],
        "pct_positive_edge_profitable_flat": pe_flat["pct"],
        "pct_positive_edge_profitable_kelly": pe_kelly["pct"],
        "positive_edge_profitable_flat": f"{pe_flat['profitable']}/{pe_flat['n']}",
        "positive_edge_profitable_kelly": f"{pe_kelly['profitable']}/{pe_kelly['n']}",
        "contrast_pct_negative_edge_profitable_flat": ne_flat["pct"],
        "contrast_pct_negative_edge_profitable_kelly": ne_kelly["pct"],
        "pnl_positive_edge_subset_flat": round(sum(r["flat_pnl"] for r in pe), 2),
        "pnl_positive_edge_subset_kelly": round(sum(r["kelly_pnl"] for r in pe), 2),
        "pnl_all_20_cities_flat": round(sum(r["flat_pnl"] for r in rows), 2),
        "pnl_all_20_cities_kelly": round(sum(r["kelly_pnl"] for r in rows), 2),
    }


META = {
    "dataset": "backtest_by_city",
    "description": "Per-city backtest of the PRODUCTION model-free strategy market_wing "
                   "(positional wing modal+adjacents, drop_lower_ask, max_ask_sum<=0.90, "
                   "assumed_win_prob=0.92, fee-aware) over each city's backfill trade history, in "
                   "both sizings: flat $2.50/trade (the live sizing) and quarter-Kelly compounding "
                   "from $1000. Joined with the prior top-2 coverage edge signal so the headline "
                   "metric is the % of previously positive-edge cities that were actually profitable.",
    "each_row_is": "one city",
    "positive_edge_signal": "edge_by_anchor.edge>0 at the 1 PM anchor under the production "
                            "max_ask_sum<=0.90 gate. That edge is a TOP-2-BY-PRICE coverage "
                            "(win_top2 - (a1+a2) - fee); the BACKTEST is the positional wing + "
                            "drop_lower_ask. Same anchor+gate, different trade -> a cross-check, "
                            "NOT a tautology.",
    "entry_truth_fees": "entry = yes_ask at the 1 PM LOCAL anchor (last-trade+1c proxy); truth = "
                        "Kalshi settlement_value; hold to settlement; Kalshi fee ceil(7% N p (1-p)) "
                        "at entry. Same engine as scripts/backtest_multicity.py.",
    "columns": {
        "city": "city label", "series": "Kalshi series ticker",
        "edge_1pm_gated": "prior top-2 edge at 1 PM under the <=0.90 gate (the positive-edge test)",
        "edge_1pm_uncond": "prior top-2 edge at 1 PM, every day (no gate) -- context",
        "positive_edge": "edge_1pm_gated > 0 (the 'previously positive-edge' flag)",
        "days": "settled event-days backtested", "n_pos": "total wing legs (positions) taken",
        "flat_wr": "per-leg win rate (flat run)", "flat_pnl": "total PnL $ at flat $2.50/trade",
        "flat_sharpe": "daily-PnL Sharpe x sqrt(252), flat", "flat_dd_usd": "max drawdown $, flat",
        "flat_profitable": "flat_pnl > 0 (the 'actually positive' flag, flat)",
        "kelly_pnl": "total PnL $ from $1000 quarter-Kelly compounding",
        "kelly_final": "ending bankroll $", "kelly_sharpe": "daily-PnL Sharpe x sqrt(252), Kelly",
        "kelly_dd_pct": "max drawdown %, Kelly", "kelly_profitable": "kelly_pnl > 0",
    },
    "caveats": [
        "in-sample, single season (spring 2026), ~66 days/city; strategy+gate chosen after seeing data",
        "assumes 100% FILL at the ask (live limit-at-ask underfills on thin books -- the real risk)",
        "ask is the last-trade+1c proxy (optimistic; tightest plausible ask)",
        "edge signal is top-2-by-price, backtest is the positional wing -- related, not identical",
        "several positive-edge cities have <20 gated days (Phoenix 12, NYC n/a, Houston 20) -> rule #4: suggestive only",
        "positive-edge set is gate-dependent: unconditional (no gate) only Chicago is edge>0",
    ],
}


def _write(rows, head):
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {**META, "headline_metrics": head}
    (OUT / "backtest_by_city.json").write_text(
        json.dumps({"_meta": meta, "data": rows}, indent=2), encoding="utf-8")
    hdr = [f"# {META['dataset']} -- {META['description']}",
           "# HEADLINE: pct_positive_edge_profitable_flat=%s  kelly=%s  (positive-edge cities: %s)"
           % (head["pct_positive_edge_profitable_flat"], head["pct_positive_edge_profitable_kelly"],
              ", ".join(head["positive_edge_cities"])),
           "# Full spec (method, positive_edge_signal, caveats, headline_metrics) in backtest_by_city.json _meta.",
           "# Columns: " + "; ".join(f"{k}={v}" for k, v in META["columns"].items()),
           "# Lines starting with # are comments (pandas: read_csv(path, comment='#'))."]
    with open(OUT / "backtest_by_city.csv", "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(hdr) + "\n")
        pd.DataFrame(rows).to_csv(f, index=False)


def _print_tables(rows, head):
    flat = sorted(rows, key=lambda r: -r["flat_pnl"])
    kelly = sorted(rows, key=lambda r: -r["kelly_pnl"])
    print("\n" + "=" * 90)
    print("FLAT $2.50/trade  --  market_wing (drop_lower_ask), the LIVE config   [PE=prior positive-edge]")
    print("=" * 90)
    print(f"{'city':<15}{'PE':>4}{'edge1pm':>9}{'days':>5}{'pos':>5}{'WR':>6}{'PnL$':>9}{'Shrp':>7}{'maxDD$':>9}")
    for r in flat:
        print(f"{r['city']:<15}{('*' if r['positive_edge'] else '.'):>4}{r['edge_1pm_gated']:>+9.3f}"
              f"{r['days']:>5}{r['n_pos']:>5}{r['flat_wr']:>6.0%}{r['flat_pnl']:>+9.2f}"
              f"{r['flat_sharpe']:>7.2f}{r['flat_dd_usd']:>+9.2f}")
    tot = sum(r["flat_pnl"] for r in rows)
    print(f"{'-'*90}\nTOTAL flat PnL ${tot:+.2f}   |   positive-edge cities profitable: "
          f"{head['positive_edge_profitable_flat']} = {head['pct_positive_edge_profitable_flat']:.0%}")

    print("\n" + "=" * 90)
    print("QUARTER-KELLY (start $1000, compounding)  --  market_wing (drop_lower_ask)")
    print("=" * 90)
    print(f"{'city':<15}{'PE':>4}{'edge1pm':>9}{'days':>5}{'pos':>5}{'PnL$':>10}{'final$':>10}{'Shrp':>7}{'maxDD%':>8}")
    for r in kelly:
        print(f"{r['city']:<15}{('*' if r['positive_edge'] else '.'):>4}{r['edge_1pm_gated']:>+9.3f}"
              f"{r['days']:>5}{r['n_pos']:>5}{r['kelly_pnl']:>+10.2f}{r['kelly_final']:>10.0f}"
              f"{r['kelly_sharpe']:>7.2f}{r['kelly_dd_pct']:>7.1f}%")
    tot = sum(r["kelly_pnl"] for r in rows)
    print(f"{'-'*90}\nTOTAL kelly PnL ${tot:+.2f}   |   positive-edge cities profitable: "
          f"{head['positive_edge_profitable_kelly']} = {head['pct_positive_edge_profitable_kelly']:.0%}")

    print("\nPositive-edge cities (1 PM, gate<=0.90): " + ", ".join(head["positive_edge_cities"]))
    print(f"  flat : {head['positive_edge_profitable_flat']} profitable = {head['pct_positive_edge_profitable_flat']:.0%}"
          f"   (vs negative-edge cities {head['contrast_pct_negative_edge_profitable_flat']:.0%})")
    print(f"  kelly: {head['positive_edge_profitable_kelly']} profitable = {head['pct_positive_edge_profitable_kelly']:.0%}"
          f"   (vs negative-edge cities {head['contrast_pct_negative_edge_profitable_kelly']:.0%})")
    print(f"  subset PnL  flat ${head['pnl_positive_edge_subset_flat']:+.2f} / kelly ${head['pnl_positive_edge_subset_kelly']:+.2f}"
          f"   vs ALL-20 flat ${head['pnl_all_20_cities_flat']:+.2f} / kelly ${head['pnl_all_20_cities_kelly']:+.2f}")


def _ensure_readme_row(n_rows: int = 20) -> None:
    """Idempotently add the backtest_by_city index row to data/analysis/README.md (the index that
    build_analysis_datasets.py generates), so this dataset is listed alongside coverage/edge."""
    p = OUT / "README.md"
    if not p.exists() or "backtest_by_city" in p.read_text(encoding="utf-8"):
        return
    row = (f"| `backtest_by_city.json` / `.csv` | {n_rows} | per-city market_wing backtest "
           "(flat + quarter-Kelly) joined with the prior positive-edge signal; headline = "
           "% of previously positive-edge cities that were profitable |")
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        out.append(ln)
        if "edge_by_anchor.json" in ln and ln.lstrip().startswith("|"):
            out.append(row)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    rows = build()
    head = headline(rows)
    _write(rows, head)
    _ensure_readme_row(len(rows))
    _print_tables(rows, head)
    print(f"\nwrote data/analysis/backtest_by_city.{{json,csv}}  ({len(rows)} cities)")


if __name__ == "__main__":
    main()
