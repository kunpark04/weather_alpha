"""Is there a 1 PM 'confidence' signal ORTHOGONAL to price that predicts the wing winning
above what the price implies? Tests intraday stability: did the 1 PM favorite bucket also
lead through the morning (8-12), or did it shift late?

If 'held-all-morning' days have a bigger gap (coverage - cost) than 'shifted' days, that's a
sizing signal the price doesn't already contain. If the gap is the same, the market prices it.
HIGH temp, fee-aware via best_anchor.day_net.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from best_anchor import day_net, AFFORD  # noqa: E402


def build(df):
    recs = []
    for (city, date), g in df.groupby(["city", "date"]):
        gi = g.set_index("hour")
        if 13 not in gi.index:
            continue
        m13 = gi.loc[13, "modal_idx"]
        morning = [gi.loc[h, "modal_idx"] for h in (8, 9, 10, 11, 12) if h in gi.index]
        if not morning:
            continue
        score = sum(1 for x in morning if x == m13) / len(morning)
        r = g[g["hour"] == 13].iloc[0]
        recs.append({"city": city, "stable": score == 1.0, "score": score,
                     "cov": int(r["wing_covers"]), "sum": float(r["wing_sum_asks"]),
                     "modal_ask": float(r["modal_ask"])})
    return pd.DataFrame(recs)


def block(sub, title):
    print(title)
    print(f"   {'group':<18}{'n':>4}{'win%':>7}{'cost':>7}{'gap':>7}{'nAff':>6}{'net/f¢':>8}")
    for name, seg in [("held-all-morning", sub[sub["stable"]]), ("shifted late", sub[~sub["stable"]])]:
        n = len(seg)
        if n == 0:
            print(f"   {name:<18}{0:>4}")
            continue
        cov, cost = seg["cov"].mean(), seg["sum"].mean()
        aff = seg[seg["sum"] < AFFORD]
        nets = [day_net(s, m, c)[0] for s, m, c in zip(aff["sum"], aff["modal_ask"], aff["cov"])]
        nf = f"{sum(nets) / len(nets) * 100:+.1f}" if nets else "  -- "
        print(f"   {name:<18}{n:>4}{cov:>7.0%}{cost:>7.2f}{(cov - cost) * 100:>+7.1f}{len(nets):>6}{nf:>8}")
    print()


def main():
    df = pd.read_parquet("data/accuracy_all.parquet")
    df = df[df["kind"] == "high"]
    d = build(df)
    print("Does an intraday-STABLE favorite win above its 1 PM price? (gap = coverage - cost)\n")
    block(d, "ALL high cities @ 1 PM (pooled)")
    block(d[d["city"] == "Chicago"], "Chicago @ 1 PM")
    print("If 'held-all-morning' gap > 'shifted late' gap, stability is an orthogonal edge.")
    print("If gaps are similar, the 1 PM price already contains the morning's information.")


if __name__ == "__main__":
    main()
