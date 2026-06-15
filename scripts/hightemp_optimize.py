"""Optimize the high-temp afternoon 'buy favorite in a price band' strategy WITH SIGNIFICANCE.

Sweeps band x entry-stamp over 4:00-6:00 PM local, with an OUT-OF-SAMPLE time split
(train = event_date < 2026-01-01, test = 2026) so a config only counts if its per-event edge is
significant in BOTH periods (guards against curve-fitting the band/stamp grid). Includes a
2nd-favorite PLACEBO (the same rule on the runner-up bucket must NOT show the edge) and a real-ask
haircut from the 13-day orderbook tape. Then sizes the winner for $-growth on a $25 account.

netEV = per-event E[win/price - 1] - fee (sizing-free). z = netEV / SE (one-sided > 1.64 ~ p<.05).
Entry price = favorite LAST TRADE <= stamp (deep backfill). Truth = Kalshi settlement_value.

Usage:
  python scripts/hightemp_optimize.py            # build cache (heavy) if absent, then optimize
  python scripts/hightemp_optimize.py --rebuild
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from scripts.late_night_coverage import HIGH, load_series, find_base

STAMPS = [(16, 0), (16, 15), (16, 30), (16, 45), (17, 0), (17, 15), (17, 30), (17, 45), (18, 0)]
BANDS = [(0.80, 0.90), (0.85, 0.90), (0.85, 0.95), (0.88, 0.93), (0.90, 0.95),
         (0.90, 0.97), (0.92, 0.97), (0.92, 0.99), (0.90, 0.99)]
CACHE = _ROOT / "data" / "hightemp_aft_events.parquet"
SPLIT = "2026-01-01"
ASK_HAIRCUT = 0.012   # mean(ask - last) on the favorite at 4-5PM from the 13-day OB tape


def lbl(hh, mm):
    return f"{hh:02d}:{mm:02d}"


def build_events(base: Path) -> pd.DataFrame:
    rows = []
    for series, (label, tz_name) in HIGH.items():
        if not (base / series).is_dir():
            continue
        df = load_series(base, series)
        if df.empty:
            continue
        tz = ZoneInfo(tz_name)
        for ev, day in df.groupby("event_date"):
            win = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
            if len(win) != 1:
                continue
            win = win[0]
            d = day.dropna(subset=["yes_price_cents"]).sort_values("timestamp")
            if d.empty:
                continue
            for hh, mm in STAMPS:
                snap = pd.Timestamp(ev.year, ev.month, ev.day, hh, mm, tz=tz).tz_convert("UTC")
                pre = d[d["timestamp"] <= snap]
                if pre.empty:
                    continue
                last = pre.groupby("ticker")["yes_price_cents"].last().sort_values(ascending=False)
                if last.size < 3:
                    continue
                fav, run = last.index[0], last.index[1]
                rows.append({"series": series, "city": label, "date": ev, "snap": lbl(hh, mm),
                             "price": last.iloc[0] / 100.0, "win": bool(fav == win),
                             "price2": last.iloc[1] / 100.0, "win2": bool(run == win)})
        print(f"  built {series} ({label})", flush=True)
    out = pd.DataFrame(rows)
    out.to_parquet(CACHE, index=False)
    return out


def net_z(price, win):
    if len(price) < 5:
        return np.nan, np.nan, len(price), np.nan
    pe = np.where(win, 1.0 / price - 1.0, -1.0)
    net = pe.mean() - 0.07 * (1.0 - price).mean()
    se = pe.std() / np.sqrt(len(pe))
    return net, (net / se if se else np.nan), len(price), float(win.mean())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args(argv)
    if CACHE.exists() and not args.rebuild:
        big = pd.read_parquet(CACHE)
        print(f"loaded cache {CACHE} ({len(big)} rows)")
    else:
        print("building per-event table 4-6PM from 3.4yr high backfill (heavy)...")
        big = build_events(find_base())
    big["date"] = pd.to_datetime(big["date"])
    tr, te = big[big.date < SPLIT], big[big.date >= SPLIT]
    print(f"train(<{SPLIT})={len(tr)} rows  test(2026)={len(te)} rows\n")

    # ---- band x stamp sweep, OOS ----
    print("=== band x stamp sweep — netEV (z) train | test ; * = significant BOTH (z>1.64) ===")
    grid = []
    for lo, hi in BANDS:
        for hh, mm in STAMPS:
            s = lbl(hh, mm)
            qtr = tr[(tr.snap == s) & (tr.price >= lo) & (tr.price <= hi)]
            qte = te[(te.snap == s) & (te.price >= lo) & (te.price <= hi)]
            ntr, ztr, Ntr, _ = net_z(qtr.price.values, qtr.win.values.astype(bool))
            nte, zte, Nte, wte = net_z(qte.price.values, qte.win.values.astype(bool))
            if np.isnan(ztr) or np.isnan(zte):
                continue
            sig = (ztr > 1.64) and (zte > 1.64)
            grid.append({"band": f"{int(lo*100)}-{int(hi*100)}", "snap": s, "ntr": ntr, "ztr": ztr,
                         "nte": nte, "zte": zte, "Nte": Nte, "wte": wte, "sig": sig,
                         "robust": min(ztr, zte)})
    g = pd.DataFrame(grid).sort_values("robust", ascending=False)
    for _, r in g.head(18).iterrows():
        mark = " *" if r["sig"] else "  "
        print(f"{mark}{r['band']:>7} @ {r['snap']}  train {r['ntr']:+.1%}(z{r['ztr']:.1f}) | "
              f"test {r['nte']:+.1%}(z{r['zte']:.1f}) N={int(r['Nte'])} wr={r['wte']:.0%}")

    sig = g[g["sig"]]
    print(f"\nconfigs significant in BOTH train & test: {len(sig)} / {len(g)}")
    if sig.empty:
        print("=> NO config holds OOS. The 4-5PM edge does not survive a train/test split.")
        return 0

    win = sig.iloc[0]
    lo, hi = [int(x)/100 for x in win["band"].split("-")]
    snap = win["snap"]
    print(f"\n=== WINNER: band {win['band']} @ {snap} ===")
    w_all = big[(big.snap == snap) & (big.price >= lo) & (big.price <= hi)]
    w_te = te[(te.snap == snap) & (te.price >= lo) & (te.price <= hi)]
    # placebo: same rule on the 2nd favorite
    pn, pz, pN, pw = net_z(w_te.price2.values, w_te.win2.values.astype(bool))
    print(f"placebo (2nd favorite, test): netEV={pn:+.1%} z={pz:.2f}  (should be ~0/negative)")
    # real-ask haircut
    p = w_te.price.values; wv = w_te.win.values.astype(bool)
    ask = np.minimum(p + ASK_HAIRCUT, 1.0)
    net_ask = (np.where(wv, 1.0/ask - 1.0, -1.0)).mean() - (0.07*(1-ask)).mean()
    print(f"test netEV @ last={win['nte']:+.1%} | @ real ASK(+{ASK_HAIRCUT*100:.1f}c)={net_ask:+.1%}  "
          f"(breakeven winrate={ask.mean()+0.07*ask.mean()*(1-ask.mean()):.1%}, actual={w_te.win.mean():.1%})")

    # ---- $-growth sizing on TEST (2026), $25, compounding ----
    print(f"\n=== $25 account on TEST (2026, {w_te.date.nunique()} days), winner config, sizings ===")
    def sim(cap_pos, frac_total, use_ask):
        bal = 25.0; curve=[bal]
        for _, day in te[te.snap == snap].groupby("date"):
            tr_d = day[(day.price >= lo) & (day.price <= hi)]
            K = len(tr_d)
            if K == 0: curve.append(bal); continue
            budget = frac_total * bal
            a = min(budget / K, cap_pos * bal)
            cost = K * a
            pay = 0.0
            for e in tr_d.itertuples():
                px = min(e.price + ASK_HAIRCUT, 1.0) if use_ask else e.price
                c = a / (px * (1 + 0.07*(1-px)))
                pay += c if e.win else 0.0
            bal = pay + (bal - cost); curve.append(bal)
        c = np.array(curve); dd = float((c/np.maximum.accumulate(c)-1).min())
        dpd = (bal/25.0)**(1/max(1,len(c)-1)) - 1
        return bal, dd, dpd
    print(f"{'sizing':<34}{'@last $':>10}{'@ask $':>10}{'maxDD':>8}{'~%/day(ask)':>12}")
    for name, cap, frac in [("split-traded full (cap100%)",1.0,1.0),("split-traded, 50%/pos cap",0.5,1.0),
                            ("total 50%/night",1.0,0.5),("total 25%/night",1.0,0.25)]:
        bl,_,_=sim(cap,frac,False); ba,da,dpa=sim(cap,frac,True)
        print(f"{name:<34}{bl:>10.2f}{ba:>10.2f}{da:>8.0%}{dpa:>+12.1%}")
    print("\n($5/day on $25 = +20%/day. Compare the ~%/day(ask) column to judge feasibility.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
