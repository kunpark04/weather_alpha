"""Anatomy of disaster days in unfiltered drop_lower_ask."""

from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from forecast_alpha.pmf import parse_bucket, parse_kalshi_subtitle, bucket_lower_bound, bucket_prob

pos = pd.read_parquet(_ROOT / "data/fullday_unfiltered_positions.parquet")
pos["date"] = pd.to_datetime(pos["date"]).dt.normalize()
day = pos.groupby("date").agg(net=("net_cents", "sum")).reset_index().sort_values("date")
day["net$"] = day["net"] / 100

disaster_days = sorted(day[day["net$"] < -100]["date"].tolist())
print(f"Disaster days (net < -$100): {len(disaster_days)}")

oof = np.load(_ROOT / 'data/model_v3_artifacts/oof_bucket_probs_calib.npy')
fold = np.load(_ROOT / 'data/model_v3_artifacts/oof_fold.npy')
feat = pd.read_parquet(_ROOT / 'data/model_v3_artifacts/feature_df.parquet').reset_index(drop=True)
valid = (fold >= 0) & ~np.isnan(oof).any(axis=1)
oof_v = oof[valid]; feat_v = feat.loc[valid].reset_index(drop=True)
kalshi = pd.read_parquet(_ROOT / 'data/kalshi_history.parquet')
kalshi['event_date'] = pd.to_datetime(kalshi['event_date']).dt.normalize()
cli = pd.read_parquet(_ROOT / 'data/cli_KMDW.parquet')
cli['date'] = pd.to_datetime(cli['date'])
truth = {pd.Timestamp(d).normalize(): int(v) for d, v in cli[['date', 'max_temp_f']].dropna().itertuples(index=False, name=None)}

for ld in disaster_days:
    idx = feat_v.index[feat_v['date'] == ld]
    if len(idx) == 0:
        print(f"{ld.date()}: no OOF\n"); continue
    i = idx[0]
    actual = truth.get(ld)

    d_kalshi = kalshi[kalshi['event_date'] == ld]
    anchor_t = (ld.tz_localize('America/Chicago') + pd.Timedelta(hours=13)).tz_convert('UTC')
    pre = d_kalshi[d_kalshi['timestamp'] <= anchor_t]
    latest = pre.sort_values('timestamp').groupby('ticker', as_index=False).last()

    contracts = []
    for _, row in latest.iterrows():
        spec = parse_kalshi_subtitle(row.get('subtitle'))
        if spec is None: continue
        yes_ask = min((row['yes_price_cents'] or 50) / 100 + 0.01, 0.99)
        p = bucket_prob(oof_v[i], spec)
        contracts.append({'spec': spec, 'yes_ask': yes_ask, 'p': p,
                          'pos_lo': bucket_lower_bound(spec), 'ticker': row['ticker']})
    contracts.sort(key=lambda c: c['pos_lo'])
    model_modal = max(contracts, key=lambda c: c['p'])
    market_modal = max(contracts, key=lambda c: c['yes_ask'])
    agreement = model_modal['spec'] == market_modal['spec']

    day_pos = pos[pos['date'] == ld]
    bought_tickers = day_pos['ticker'].tolist()
    bought_specs = []
    for t in bought_tickers:
        c = next((c for c in contracts if c['ticker'] == t), None)
        if c: bought_specs.append(c['spec'])

    winner_spec = None
    for c in contracts:
        pred, _ = parse_bucket(c['spec'])
        if pred(actual): winner_spec = c['spec']; break

    net = day[day['date'] == ld]['net$'].iloc[0]
    print(f"=== {ld.date()}: actual={actual}F, net=${net:+.2f} ===")
    print(f"  Agreement: {agreement}  (model_modal={model_modal['spec']} p={model_modal['p']:.3f}, market_modal={market_modal['spec']} ask={market_modal['yes_ask']:.2f})")
    print(f"  Bought: {bought_specs}")
    print(f"  Winner bucket: {winner_spec}")
    print()
