"""Diagnose the 2 late-half losses for drop_lower_ask."""

from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from forecast_alpha.pmf import parse_bucket, parse_kalshi_subtitle, bucket_lower_bound, bucket_prob

pos = pd.read_parquet(_ROOT / "data/oos_full_positions.parquet")
pos["date"] = pd.to_datetime(pos["date"]).dt.normalize()

# Per-day net + n_legs
day = pos.groupby("date").agg(net=("net_cents", "sum"), n_legs=("ticker", "count")).reset_index().sort_values("date")
day["net_dollars"] = day["net"] / 100

print("All 22 drop_lower_ask trade days, chronological:")
print(f"{'#':>2} {'date':<12} {'legs':>4} {'net$':>10}")
for i, (_, r) in enumerate(day.iterrows(), 1):
    half = "EARLY" if i <= 11 else "LATE "
    print(f"{i:>2} {r['date'].date()} {half} {r['n_legs']:>4} {r['net_dollars']:>+10.2f}")

print()
losing_days = day[day["net"] < 0]["date"].tolist()
print(f"Loss days: {[d.date() for d in losing_days]}")
print()

# Now anatomy for each loss
oof = np.load(_ROOT / 'data/model_v3_artifacts/oof_bucket_probs_calib.npy')
fold = np.load(_ROOT / 'data/model_v3_artifacts/oof_fold.npy')
feat = pd.read_parquet(_ROOT / 'data/model_v3_artifacts/feature_df.parquet').reset_index(drop=True)
valid = (fold >= 0) & ~np.isnan(oof).any(axis=1)
oof_v = oof[valid]
feat_v = feat.loc[valid].reset_index(drop=True)
kalshi = pd.read_parquet(_ROOT / 'data/kalshi_history.parquet')
kalshi['event_date'] = pd.to_datetime(kalshi['event_date']).dt.normalize()
cli = pd.read_parquet(_ROOT / 'data/cli_KMDW.parquet')
cli['date'] = pd.to_datetime(cli['date'])
truth = {pd.Timestamp(d).normalize(): int(v) for d, v in cli[['date', 'max_temp_f']].dropna().itertuples(index=False, name=None)}

for ld in losing_days:
    idx = feat_v.index[feat_v['date'] == ld]
    if len(idx) == 0:
        print(f"=== {ld.date()}: no OOF row ===\n"); continue
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

    model_modal_idx = max(range(len(contracts)), key=lambda j: contracts[j]['p'])
    market_modal_idx = max(range(len(contracts)), key=lambda j: contracts[j]['yes_ask'])

    # Wing = modal + adjacents
    wing_indices = [model_modal_idx]
    if model_modal_idx > 0: wing_indices.append(model_modal_idx - 1)
    if model_modal_idx < len(contracts) - 1: wing_indices.append(model_modal_idx + 1)

    # The dropped-lower-ask logic
    adjacents_in_wing = [j for j in wing_indices if j != model_modal_idx]
    adjacents_in_wing.sort(key=lambda j: -contracts[j]['yes_ask'])
    higher_ask_adj_idx = adjacents_in_wing[0] if adjacents_in_wing else None
    lower_ask_adj_idx = adjacents_in_wing[-1] if len(adjacents_in_wing) > 1 else None

    # Find the actual winning contract
    winner_idx = None
    for j, c in enumerate(contracts):
        pred, _ = parse_bucket(c['spec'])
        if pred(actual):
            winner_idx = j; break

    net_day = day[day["date"] == ld]["net_dollars"].iloc[0]
    print(f"=== {ld.date()}: actual={actual}F, day_net=${net_day:+.2f} ===")
    print(f"  Model modal:  {contracts[model_modal_idx]['spec']}  (p={contracts[model_modal_idx]['p']:.3f})")
    print(f"  Market modal: {contracts[market_modal_idx]['spec']}  (ask={contracts[market_modal_idx]['yes_ask']:.2f})")
    print(f"  Agreement (modal): {contracts[model_modal_idx]['spec'] == contracts[market_modal_idx]['spec']}")
    if higher_ask_adj_idx is not None:
        print(f"  Higher-ask adj: {contracts[higher_ask_adj_idx]['spec']}  (ask={contracts[higher_ask_adj_idx]['yes_ask']:.2f})  <- WE KEPT")
    if lower_ask_adj_idx is not None:
        print(f"  Lower-ask adj:  {contracts[lower_ask_adj_idx]['spec']}  (ask={contracts[lower_ask_adj_idx]['yes_ask']:.2f})  <- WE DROPPED")
    if winner_idx is not None:
        w = contracts[winner_idx]
        role = ("MODAL" if winner_idx == model_modal_idx else
                "HIGHER-ASK-ADJ (we kept)" if winner_idx == higher_ask_adj_idx else
                "LOWER-ASK-ADJ (we dropped!)" if winner_idx == lower_ask_adj_idx else
                "OUTSIDE WING")
        print(f"  Winner: {w['spec']}  ({role})")
    else:
        print(f"  Winner: actual {actual}F outside all buckets")
    print()
