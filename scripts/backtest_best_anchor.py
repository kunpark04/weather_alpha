"""Fee-aware backtest of the production wing (market_wing + drop_lower_ask, equal-payout,
flat $2.50) at EACH high-temp city's best anchor, with a drop_higher_ask directional
placebo. Reconstructed from data/accuracy_all.parquet (fills at the hour's yes_ask).

Production wing per affordable day: net via best_anchor.day_net (real Kalshi fees).
Placebo (drop_higher_ask = keep the LOWER-ask adjacent) coverage is derivable exactly:
    placebo_covers = top1 OR (top3 AND NOT wing_covers)
so WR - placebo_WR = P(higher-ask adjacent wins) - P(lower-ask adjacent wins): the exact
directional edge the strategy claims. adj_edge = of adjacent-win days, share won by the
higher-ask (production) adjacent; > 50% confirms the market's ask-ranking is informative.

Caveats: candlestick reconstruction; integer-K sizing approx; only the affordability gate
(not the full run_wing_strategy gate set); placebo is coverage-exact but not P&L-exact.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from best_anchor import day_net, STAKE, AFFORD, MIN_FIRES  # noqa: E402


def max_drawdown(nets) -> float:
    cum = peak = dd = 0.0
    for x in nets:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    df = df[df["kind"] == "high"].copy()
    aff = df[df["wing_sum_asks"] < AFFORD].copy()
    aff["net"] = [day_net(s, m, c)[0] for s, m, c in
                  zip(aff["wing_sum_asks"], aff["modal_ask"], aff["wing_covers"])]
    aff["placebo_cov"] = ((aff["top1"] == 1) | ((aff["top3"] == 1) & (aff["wing_covers"] == 0))).astype(int)

    # best anchor per city = argmax total net (n_fires >= MIN_FIRES)
    g = aff.groupby(["city", "hour"]).agg(n=("net", "size"), total=("net", "sum")).reset_index()
    best = {}
    for city, gc in g.groupby("city"):
        elig = gc[gc["n"] >= MIN_FIRES]
        src = elig if not elig.empty else gc
        best[city] = int(src.loc[src["total"].idxmax(), "hour"])

    rows = []
    for city, h in best.items():
        d = aff[(aff.city == city) & (aff.hour == h)].sort_values("date")
        nets = d["net"].tolist()
        wr, pwr = d["wing_covers"].mean(), d["placebo_cov"].mean()
        adj = d[(d["top3"] == 1) & (d["top1"] == 0)]                 # winner was an adjacent
        adj_edge = adj["wing_covers"].mean() if len(adj) else float("nan")
        pnl = sum(nets)
        staked = len(d) * STAKE
        rows.append({"city": city, "anchor": f"{h}:00", "fires": len(d), "wr": wr,
                     "pnl": pnl, "roi": pnl / staked if staked else 0.0,
                     "net_fire": pnl / len(d) if nets else 0.0, "maxdd": max_drawdown(nets),
                     "pwr": pwr, "adj_n": len(adj), "adj_edge": adj_edge})
    res = pd.DataFrame(rows).sort_values("pnl", ascending=False)

    def verdict(r):
        if r["pnl"] <= 0:
            return "LOSS"
        if r["wr"] <= r["pwr"] or (r["adj_edge"] == r["adj_edge"] and r["adj_edge"] <= 0.5):
            return "no direction"
        if r["fires"] < 15:
            return "edge? (thin)"
        return "EDGE"

    print("=" * 96)
    print("FEE-AWARE BACKTEST @ best anchor  -  HIGH temp, $2.50 equal-payout drop_lower_ask wing")
    print("=" * 96)
    print(f"{'city':<15}{'anchor':>7}{'fires':>6}{'WR%':>6}{'PnL$':>8}{'ROI%':>7}"
          f"{'net/f':>7}{'maxDD$':>8}{'plcboWR':>8}{'adjEdge':>9}  verdict")
    for _, r in res.iterrows():
        ae = "  --  " if r["adj_edge"] != r["adj_edge"] else f"{r['adj_edge']:4.0%}({r['adj_n']})"
        print(f"{r['city']:<15}{r['anchor']:>7}{r['fires']:>6}{r['wr']:>6.0%}{r['pnl']:>+8.2f}"
              f"{r['roi']:>+7.1%}{r['net_fire'] * 100:>+6.1f}{r['maxdd']:>8.2f}{r['pwr']:>8.0%}"
              f"{ae:>9}  {verdict(r)}")
    tot = res["pnl"].sum()
    print(f"\nAggregate (one $2.50 wing/day per city at its best anchor): {res['fires'].sum()} fires, "
          f"net {tot:+.2f}$ over the window.")
    print("WR=production wing coverage; plcboWR=drop_higher_ask coverage; "
          "adjEdge=of adjacent-win days, % won by the higher-ask (prod) adjacent (>50% => edge).")
    res.to_csv("data/backtest_best_anchor.csv", index=False)
    print("full -> data/backtest_best_anchor.csv")


if __name__ == "__main__":
    main()
