"""Why are most high-temp cities unprofitable at the 1 PM anchor?

The fee-aware 2-leg equal-payout wing is +EV iff:
    win% (realized coverage)  >  cost (sum of the two legs' asks)  +  fee drag (~3 pts)
And since ask ~= market-implied probability, `cost` ~= the market's OWN implied probability
that the wing contains the winner. So the wing only profits where the market UNDERPRICES its
true coverage (realized win% > implied cost). This prints the gap per city at 1 PM.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from best_anchor import day_net, AFFORD  # noqa: E402


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    g13 = df[(df["kind"] == "high") & (df["hour"] == 13)]
    rows = []
    for city, g in g13.groupby("city"):
        aff = g[g["wing_sum_asks"] < AFFORD]
        nets = [day_net(s, m, c)[0] for s, m, c in
                zip(aff["wing_sum_asks"], aff["modal_ask"], aff["wing_covers"])]
        n = len(nets)
        win = aff["wing_covers"].mean() if n else float("nan")
        cost = aff["wing_sum_asks"].mean() if n else float("nan")
        fav = aff["modal_ask"].mean() if n else float("nan")
        rows.append({"city": city, "fires": n, "fav_ask": fav, "win": win, "cost": cost,
                     "gap": (win - cost) if n else float("nan"),
                     "netf": (sum(nets) / n * 100) if n else float("nan"),
                     "untradeable_frac": (g["wing_sum_asks"] >= AFFORD).mean()})
    t = pd.DataFrame(rows).sort_values("gap", ascending=False)

    print("=" * 92)
    print("WHY 1 PM: realized coverage (win%) vs market-implied cost (sum of leg asks). Need gap > ~+3 pts.")
    print("=" * 92)
    print(f"{'city':<15}{'fires':>6}{'favAsk':>8}{'win%':>7}{'cost':>7}{'gap(win-cost)':>15}{'net/f¢':>9}  why")
    for _, r in t.iterrows():
        if r["fires"] == 0:
            why = "favorite too pricey -> wing never < $0.90 (0 fires)"
        elif r["gap"] != r["gap"]:
            why = ""
        elif r["gap"] > 0.05:
            why = "PROFITABLE: coverage underpriced by the market"
        elif r["cost"] >= 0.85:
            why = "too expensive: favorite priced high -> break-even ~90%"
        else:
            why = "too efficient: realized coverage ~= cost -> fees sink it"
        gp = "  --  " if r["gap"] != r["gap"] else f"{r['gap']:+6.1%}"
        wn = "  -- " if r["win"] != r["win"] else f"{r['win']:5.0%}"
        cs = "  -- " if r["cost"] != r["cost"] else f"{r['cost']:5.2f}"
        fv = "  -- " if r["fav_ask"] != r["fav_ask"] else f"{r['fav_ask']:5.2f}"
        nf = "  --  " if r["netf"] != r["netf"] else f"{r['netf']:+6.1f}"
        print(f"{r['city']:<15}{r['fires']:>6}{fv:>8}{wn:>7}{cs:>7}{gp:>15}{nf:>9}  {why}")
    print("\ngap = realized win% minus what you paid (sum of leg asks). Fees need gap > ~+3 pts.")
    print("ask ~= market-implied prob, so cost ~= the market's OWN P(winner in wing); a +gap means the market underprices the wing's true coverage.")


if __name__ == "__main__":
    main()
