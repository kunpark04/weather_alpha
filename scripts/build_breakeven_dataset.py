"""Build data/analysis/breakeven_coverage_by_city.{json,csv}: the BREAKEVEN COVERAGE per city --
the wingCov at which the wing's edge = 0, which equals cost + fee (you must cover the winner often
enough to pay back what you spent). margin = actual wingCov - breakeven = edge.

HEADLINE: breakeven sits in a tight ~85% band (cost+fee is nearly uniform across cities), so every
city must cover ~85% of fired days just to break even and edge ~= wingCov - 85%; the outcome spread
is almost entirely coverage, with the thin breakeven variation deciding the close calls. Built from
the same instrumented wing pass as scripts/rca_profitable_vs_losing.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from scripts.rca_profitable_vs_losing import rca_city
from weather_alpha.config import load_config

OUT = _ROOT / "data" / "analysis"


def build():
    cfg = load_config()
    rows = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        r = rca_city(df, s, cfg)
        if not r:
            continue
        rows.append({
            "city": r["city"], "series": s,
            "mean_cost": r["mean_cost"], "mean_fee": r["mean_fee"],
            "breakeven_cov": r["breakeven"],          # = cost + fee = wingCov needed for edge=0
            "actual_wing_cov": r["coverage"],
            "margin": r["edge"],                      # actual_wing_cov - breakeven_cov
            "profitable": bool(r["profitable"]), "flat_pnl": r["flat_pnl"],
        })
    return pd.DataFrame(rows).sort_values("margin", ascending=False)


def headline(d):
    be = d["breakeven_cov"]; win = d[d.margin > 0]; los = d[d.margin <= 0]
    return {
        "breakeven_definition": "breakeven_cov = mean_cost + mean_fee (per $1 of cover) on FIRED days "
                                "= the wingCov at which the wing's edge is exactly 0.",
        "breakeven_band_min": round(float(be.min()), 3), "breakeven_band_min_city": d.loc[be.idxmin(), "city"],
        "breakeven_band_max": round(float(be.max()), 3), "breakeven_band_max_city": d.loc[be.idxmax(), "city"],
        "breakeven_band_mean": round(float(be.mean()), 3), "breakeven_band_std": round(float(be.std()), 3),
        "headline": (f"Breakeven coverage sits in a TIGHT band: {be.min():.1%} ({d.loc[be.idxmin(),'city']}, "
                     f"cheapest) to {be.max():.1%} ({d.loc[be.idxmax(),'city']}, priciest), mean {be.mean():.1%}, "
                     f"std {be.std()*100:.1f}pts. Because cost+fee is nearly uniform, every city must cover "
                     f"~85% of fired days to break even and edge ~= wingCov - 85%; the outcome spread is "
                     f"almost all coverage, with the thin breakeven variation deciding close calls (SF's low "
                     f"{be.min():.1%} bar survives weak coverage; NYC's high {be.max():.1%} bar loses despite "
                     f"decent coverage)."),
        "n_above_breakeven": int((d.margin > 0).sum()),
        "mean_margin_winners": round(float(win.margin.mean()), 3),
        "mean_margin_losers": round(float(los.margin.mean()), 3),
    }


META = {
    "dataset": "breakeven_coverage_by_city",
    "description": "Per-city BREAKEVEN COVERAGE for the production wing: the gated wingCov at which "
                   "edge = 0 (= cost + fee), the actual gated wingCov, and the margin between them "
                   "(= edge). Answers 'how often must each city's wing contain the winner just to "
                   "not lose money, and by how much does it clear or miss that bar?'.",
    "each_row_is": "one city (sorted by margin = edge, descending)",
    "columns": {
        "city": "city label", "series": "Kalshi series ticker",
        "mean_cost": "mean cost to cover (sum of the 2 leg asks, per $1 payout) on FIRED days",
        "mean_fee": "mean two-leg Kalshi fee (per $1 payout) on fired days",
        "breakeven_cov": "mean_cost + mean_fee = the wingCov needed for edge = 0 (the breakeven bar)",
        "actual_wing_cov": "realized coverage on fired days (winner inside the 2-leg wing)",
        "margin": "actual_wing_cov - breakeven_cov = edge (>0 profitable in expectation)",
        "profitable": "backtest flat $2.50 PnL > 0 (realized; can disagree with margin's sign by noise)",
        "flat_pnl": "backtest flat $2.50/trade PnL $ over the season",
    },
    "caveats": ["in-sample single season (spring 2026), ~24-45 fired days/city (LA=1 -> 0% coverage)",
                "cost uses the last-trade+1c ask proxy -> breakeven is an OPTIMISTIC lower bound; real "
                "spreads push the true breakeven HIGHER (edge lower)",
                "cities within ~+-3% of breakeven (SF, Denver, Houston, NYC) are statistical coin-flips "
                "-> profitable flag and margin sign can disagree on them"],
}


def _write(d, head):
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {**META, "headline_metrics": head}
    (OUT / "breakeven_coverage_by_city.json").write_text(
        json.dumps({"_meta": meta, "data": d.to_dict("records")}, indent=2), encoding="utf-8")
    hdr = [f"# {META['dataset']} -- {META['description']}",
           "# HEADLINE: " + head["headline"],
           "# Full spec (columns, headline_metrics, caveats) in the matching .json _meta.",
           "# Columns: " + "; ".join(f"{k}={v}" for k, v in META["columns"].items()),
           "# Lines starting with # are comments (pandas: read_csv(path, comment='#'))."]
    with open(OUT / "breakeven_coverage_by_city.csv", "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(hdr) + "\n")
        d.to_csv(f, index=False)


def _ensure_readme_row(n_rows):
    p = OUT / "README.md"
    txt = p.read_text(encoding="utf-8") if p.exists() else ""
    if not txt or "breakeven_coverage_by_city" in txt:
        return
    row = (f"| `breakeven_coverage_by_city.json` / `.csv` | {n_rows} | per-city breakeven coverage "
           "(cost+fee) vs actual gated wingCov + margin; how much each city clears/misses its bar |")
    anchor = "coverage_indicator_by_city.json" if "coverage_indicator_by_city" in txt else "backtest_by_city.json"
    out = []
    for ln in txt.splitlines():
        out.append(ln)
        if anchor in ln and ln.lstrip().startswith("|"):
            out.append(row)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    d = build()
    head = headline(d)
    _write(d, head)
    _ensure_readme_row(len(d))
    print(f"{'city':<15}{'breakeven':>11}{'wingCov':>10}{'margin':>9}{'profit':>8}")
    for _, r in d.iterrows():
        print(f"{r['city']:<15}{r['breakeven_cov']:>10.1%}{r['actual_wing_cov']:>10.1%}"
              f"{r['margin']:>+9.1%}{('Y' if r['profitable'] else '-'):>8}")
    print("\nHEADLINE: " + head["headline"])
    print(f"\nwrote data/analysis/breakeven_coverage_by_city.{{json,csv}} ({len(d)} cities)")


if __name__ == "__main__":
    main()
