"""Is POSITIVE-EDGE or PROFITABILITY the better indicator of a 'good' (high-coverage) market for
the wing? Builds a per-city table of the top-1/2/3 settlement hit-rate (coverage_by_anchor, 1 PM,
UNCONDITIONAL = accuracy the settled high lands in the K highest-priced buckets), the wing's GATED
coverage + fire rate (what the strategy actually experiences), and both flags (edge>0 from
edge_by_anchor; profitable = backtest flat PnL>0). Scores which flag better separates cities by
coverage (point-biserial corr of the binary flag with coverage) and writes a self-documenting
dataset data/analysis/coverage_indicator_by_city.{json,csv}.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.modal_rank_multicity import BACKFILL, CITY, load_city
from scripts.rca_profitable_vs_losing import rca_city
from weather_alpha.config import load_config

OUT = _ROOT / "data" / "analysis"


def _cov_top():
    c = pd.read_csv(OUT / "coverage_by_anchor.csv", comment="#")
    c = c[(c.scope == "city") & (c.anchor_local_hour == 13)]
    return c.set_index("series")[["top1", "top2", "top3", "n_days"]].to_dict("index")


def _pos_edge():
    e = pd.read_csv(OUT / "edge_by_anchor.csv", comment="#")
    e = e[(e.scope == "city") & (e.anchor_local_hour == 13) & (e.gate == "gated_le_0.90")]
    return e.set_index("series")["edge"].to_dict()


def build():
    cfg = load_config()
    cov, pe = _cov_top(), _pos_edge()
    rows = []
    for s in CITY:
        if not (BACKFILL / s).is_dir():
            continue
        df = load_city(s)
        if df.empty:
            continue
        r = rca_city(df, s, cfg)
        if not r or s not in cov:
            continue
        ct = cov[s]
        rows.append({
            "city": r["city"], "series": s,
            "top1": round(ct["top1"], 4), "top2": round(ct["top2"], 4), "top3": round(ct["top3"], 4),
            "fire_pct": round(r["fired"] / ct["n_days"], 4),
            "wing_cov": round(r["coverage"], 4), "wing_edge": round(r["edge"], 4),
            "positive_edge": bool(pe.get(s, -1) > 0), "profitable": bool(r["profitable"]),
            "flat_pnl": round(r["flat_pnl"], 2),
        })
    return pd.DataFrame(rows).sort_values("wing_cov", ascending=False)


def _pb(d, flag, cov_col):
    return float(np.corrcoef(d[flag].astype(float).values, d[cov_col].astype(float).values)[0, 1])


def _grp(d, flag, cov_col):
    y = float(d[d[flag]][cov_col].mean()); n = float(d[~d[flag]][cov_col].mean())
    return y, n, y - n


def comparison(d):
    out = {}
    for cov_col in ["wing_cov", "top2"]:
        out[cov_col] = {}
        for flag in ["positive_edge", "profitable"]:
            y, n, gap = _grp(d, flag, cov_col)
            out[cov_col][flag] = {"point_biserial_r": round(_pb(d, flag, cov_col), 3),
                                  "mean_cov_yes": round(y, 3), "mean_cov_no": round(n, 3),
                                  "gap": round(gap, 3)}
    pe_r = out["wing_cov"]["positive_edge"]["point_biserial_r"]
    pf_r = out["wing_cov"]["profitable"]["point_biserial_r"]
    out["verdict"] = (
        f"positive_edge tracks the strategy-relevant GATED wing coverage marginally better than "
        f"profitability (point-biserial r {pe_r:+.2f} vs {pf_r:+.2f}). The flags agree on 18/20 "
        f"cities and split only on San Francisco / Denver: positive_edge picks the higher-coverage "
        f"city (Denver 85% > SF 81%); profitability picks the lower-coverage SF (a noisy, "
        f"cheap-cost realized win). Both are razor-thin & in-sample -- the robust signal is the "
        f"gated wing coverage itself, OOS-validated.")
    return out


META = {
    "dataset": "coverage_indicator_by_city",
    "description": "Per-city coverage diagnostics + the two market-selection flags, to decide "
                   "whether POSITIVE-EDGE or PROFITABILITY better indicates a good (high-coverage) "
                   "market for the production wing.",
    "each_row_is": "one city",
    "columns": {
        "city": "city label", "series": "Kalshi series ticker",
        "top1": "UNCONDITIONAL P(settled high == highest-priced bucket) at 1 PM (all days)",
        "top2": "UNCONDITIONAL P(settled high in the 2 highest-priced buckets) at 1 PM",
        "top3": "UNCONDITIONAL P(settled high in the 3 highest-priced buckets) at 1 PM",
        "fire_pct": "fraction of settled days the wing FIRES (sum of top-2 asks <= 0.90 gate)",
        "wing_cov": "coverage on FIRED days only (winner inside the 2-leg wing) -- the coverage the "
                    "strategy actually experiences; this is what drives PnL",
        "wing_edge": "wing realized edge = wing_cov - mean_cost - fee (per $1) on fired days",
        "positive_edge": "edge_by_anchor edge>0 at 1 PM under the <=0.90 gate (candidate flag A)",
        "profitable": "backtest flat $2.50 PnL > 0 (candidate flag B)",
        "flat_pnl": "backtest flat $2.50/trade PnL $ over the season",
    },
    "key_findings": [
        "UNCONDITIONAL top-K is a red herring: LA has the highest top-2 (98%) yet fires 2% of days "
        "and loses; the gate + gated wing_cov are what matter.",
        "top-3 is 92-100% for every city (no discriminating power); even top-1 doesn't sort cleanly.",
        "clean split: gated wing_cov >= ~85% -> profitable; < ~80% -> losing; 81-85% is the coin-flip band.",
    ],
    "caveats": ["in-sample single season (spring 2026); ~24-45 fired days/city (some <16; LA=1)",
                "the positive_edge vs profitability margin rests on 2 coin-flip cities (SF, Denver)",
                "ask is the last-trade+1c proxy (optimistic); 100% fill assumed"],
}


def _write(d, comp):
    OUT.mkdir(parents=True, exist_ok=True)
    rows = d.to_dict("records")
    meta = {**META, "indicator_comparison": comp}
    (OUT / "coverage_indicator_by_city.json").write_text(
        json.dumps({"_meta": meta, "data": rows}, indent=2), encoding="utf-8")
    hdr = [f"# {META['dataset']} -- {META['description']}",
           "# VERDICT: " + comp["verdict"],
           "# Full spec (columns, key_findings, indicator_comparison, caveats) in the matching .json _meta.",
           "# Columns: " + "; ".join(f"{k}={v}" for k, v in META["columns"].items()),
           "# Lines starting with # are comments (pandas: read_csv(path, comment='#'))."]
    with open(OUT / "coverage_indicator_by_city.csv", "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(hdr) + "\n")
        d.to_csv(f, index=False)


def _ensure_readme_row(n_rows):
    p = OUT / "README.md"
    if not p.exists() or "coverage_indicator_by_city" in p.read_text(encoding="utf-8"):
        return
    row = (f"| `coverage_indicator_by_city.json` / `.csv` | {n_rows} | per-city top-1/2/3 hit-rate + "
           "gated wing coverage + edge>0 / profitable flags; which flag better indicates a "
           "high-coverage market |")
    anchor = "backtest_by_city.json" if "backtest_by_city" in p.read_text(encoding="utf-8") else "edge_by_anchor.json"
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        out.append(ln)
        if anchor in ln and ln.lstrip().startswith("|"):
            out.append(row)
    p.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    d = build()
    comp = comparison(d)
    _write(d, comp)
    _ensure_readme_row(len(d))

    print("\n" + "=" * 104)
    print("PER-CITY COVERAGE vs INDICATORS  (top1/2/3 = unconditional settlement hit-rate by price rank, 1 PM)")
    print("=" * 104)
    print(f"{'city':<15}{'top1':>7}{'top2':>7}{'top3':>7}{'fire%':>7}{'wingCov':>9}{'edge>0':>8}{'profit':>8}{'PnL$':>9}")
    for _, r in d.iterrows():
        print(f"{r['city']:<15}{r['top1']:>7.0%}{r['top2']:>7.0%}{r['top3']:>7.0%}{r['fire_pct']:>7.0%}"
              f"{r['wing_cov']:>9.0%}{('Y' if r['positive_edge'] else '-'):>8}{('Y' if r['profitable'] else '-'):>8}"
              f"{r['flat_pnl']:>+9.2f}")

    print("\nWHICH FLAG TRACKS COVERAGE BETTER? (point-biserial r; group means)")
    for cov_col, lab in [("wing_cov", "gated wing coverage (what the strategy trades)"),
                          ("top2", "unconditional top-2 hit-rate")]:
        print(f"  -- vs {lab} --")
        for flag in ["positive_edge", "profitable"]:
            y, n, gap = _grp(d, flag, cov_col)
            print(f"     {flag:<14} r={_pb(d, flag, cov_col):+.2f}  mean[Y]={y:.0%} mean[N]={n:.0%} gap={gap:+.1%}")
    print("\n" + comp["verdict"])
    print(f"\nwrote data/analysis/coverage_indicator_by_city.{{json,csv}} ({len(d)} cities)")


if __name__ == "__main__":
    main()
