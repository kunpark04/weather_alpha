"""Cumulative top-k settlement hit-rate by MARKET RANK, swept across anchor hours.

For each anchor hour h (local CT) and each Kalshi day, take the last quote <= h:00 CT,
rank buckets by yes_ask (rank-1 = market modal, rank-2 = 2nd highest, ...), and measure
how often the SETTLED bucket (CLI daily high) fell in the top-1 / top-2 / top-3 asks.
Shown unconditionally and conditioned on MODEL AGREEMENT (market modal == model modal;
the model PMF is the single daily 1 PM prediction, fixed across hours).

This traces the intraday CONVERGENCE: as the day's temperature realizes, the market's
cumulative cover sharpens. N per hour is reported because early-morning liquidity is thin
and the sample self-selects toward days that happened to trade that early.

NOTE: kalshi_history.parquet carries last-trade price, not depth; yes_ask is the project's
last_cents/100 + 0.01 proxy (cap 0.99). Fine for RANK; not exact prices.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from weather_alpha.pmf import parse_bucket, parse_kalshi_subtitle, bucket_prob

ANCHOR_HOURS = range(6, 17)   # 6 AM .. 4 PM local CT

oof = np.load(_ROOT / "data/model_v3_artifacts/oof_bucket_probs_calib.npy")
fold = np.load(_ROOT / "data/model_v3_artifacts/oof_fold.npy")
feat = pd.read_parquet(_ROOT / "data/model_v3_artifacts/feature_df.parquet").reset_index(drop=True)
tgt = pd.read_parquet(_ROOT / "data/model_v3_artifacts/target_df.parquet").reset_index(drop=True)

valid = (fold >= 0) & ~np.isnan(oof).any(axis=1) & ~tgt["cli_high"].isna()
probs = oof[valid]
dates = pd.to_datetime(feat.loc[valid, "date"]).dt.normalize()
date_to_idx = {d: i for i, d in enumerate(dates)}

kalshi = pd.read_parquet(_ROOT / "data/kalshi_history.parquet")
kalshi["event_date"] = pd.to_datetime(kalshi["event_date"]).dt.normalize()
kalshi["timestamp"] = pd.to_datetime(kalshi["timestamp"], utc=True)

cli = pd.read_parquet(_ROOT / "data/cli_KMDW.parquet")
cli["date"] = pd.to_datetime(cli["date"]).dt.normalize()
truth = {d: int(t) for d, t in cli[["date", "max_temp_f"]].dropna().itertuples(index=False, name=None)}

# Pre-group trades by event date once.
ev_days = [pd.Timestamp(d).normalize() for d in sorted(kalshi["event_date"].unique())]
ev_days = [d for d in ev_days if d in date_to_idx and d in truth]
by_day = {d: g.sort_values("timestamp") for d, g in kalshi.groupby("event_date") if d in date_to_idx and d in truth}


def day_records(hour: int):
    out = []
    for ev in ev_days:
        g = by_day.get(ev)
        if g is None:
            continue
        anchor_t = (ev.tz_localize("America/Chicago") + pd.Timedelta(hours=hour)).tz_convert("UTC")
        pre = g[g["timestamp"] <= anchor_t]
        if pre.empty:
            continue
        latest = pre.groupby("ticker", as_index=False).last()
        pmf = probs[date_to_idx[ev]]
        contracts = []
        for _, row in latest.iterrows():
            spec = parse_kalshi_subtitle(row.get("subtitle"))
            if spec is None:
                continue
            yes_ask = min((row["yes_price_cents"] or 50) / 100 + 0.01, 0.99)
            contracts.append({"spec": spec, "yes_ask": yes_ask, "p_model": bucket_prob(pmf, spec)})
        if len(contracts) < 3:
            continue
        actual_idx = next((j for j, c in enumerate(contracts)
                           if parse_bucket(c["spec"])[0](truth[ev])), None)
        if actual_idx is None:
            continue
        mkt = sorted(range(len(contracts)), key=lambda j: -contracts[j]["yes_ask"])
        mdl = sorted(range(len(contracts)), key=lambda j: -contracts[j]["p_model"])
        out.append({
            "agreement": mkt[0] == mdl[0],
            "t1": actual_idx == mkt[0],
            "t2": actual_idx in mkt[:2],
            "t3": actual_idx in mkt[:3],
        })
    return pd.DataFrame(out)


print("=" * 78)
print("Cumulative top-k settlement hit-rate by MARKET rank, by anchor hour (CT)")
print("  KXHIGHCHI / KMDW   |   settles in top-k highest-ask buckets")
print("=" * 78)
print(f"{'hour':>5} | {'N':>3} {'top-1':>7}{'top-2':>7}{'top-3':>7}  |  "
      f"{'agreeN':>6} {'top-1':>7}{'top-2':>7}{'top-3':>7}")
print(f"{'':>5} | {'  ALL DAYS':<26}  |  {'  AGREEMENT DAYS ONLY':<28}")
print("-" * 78)
for h in ANCHOR_HOURS:
    d = day_records(h)
    if d.empty:
        print(f"{h:>3}:00 | (no days)")
        continue
    ag = d[d["agreement"]]
    lbl = f"{h:>3}:00"
    if h == 13:
        lbl += "*"
    a = (f"{len(ag):>6} {ag['t1'].mean():>7.0%}{ag['t2'].mean():>7.0%}{ag['t3'].mean():>7.0%}"
         if len(ag) else f"{0:>6} {'--':>7}{'--':>7}{'--':>7}")
    print(f"{lbl:>5} | {len(d):>3} {d['t1'].mean():>7.0%}{d['t2'].mean():>7.0%}{d['t3'].mean():>7.0%}  |  {a}")
print("-" * 78)
print("* 13:00 = the production 1 PM anchor.  top-k = settled bucket among the k highest asks.")
print("Early hours: smaller N (thin pre-market liquidity) => accuracy is on a self-selected subset.")
