"""For each prob-weighted wing losing day, break down the full anatomy:
- All 6 contracts with their position, p_model, yes_ask
- Which leg was the model's modal / wing-low / wing-high
- The actual outcome
- Why this resulted in a net loss
"""

from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from weather_alpha.pmf import (
    parse_bucket, parse_kalshi_subtitle, bucket_lower_bound, bucket_prob,
)

oof = np.load(_ROOT / 'data/model_v3_artifacts/oof_bucket_probs_calib.npy')
fold = np.load(_ROOT / 'data/model_v3_artifacts/oof_fold.npy')
feat = pd.read_parquet(_ROOT / 'data/model_v3_artifacts/feature_df.parquet').reset_index(drop=True)
valid = (fold >= 0) & ~np.isnan(oof).any(axis=1)
oof_v = oof[valid]; feat_v = feat.loc[valid].reset_index(drop=True)

kalshi = pd.read_parquet(_ROOT / 'data/kalshi_history.parquet')
kalshi['event_date'] = pd.to_datetime(kalshi['event_date']).dt.normalize()
cli = pd.read_parquet(_ROOT / 'data/cli_KMDW.parquet')
cli['date'] = pd.to_datetime(cli['date'])
truth = {pd.Timestamp(d).normalize(): int(v) for d, v in
         cli[['date', 'max_temp_f']].dropna().itertuples(index=False, name=None)}

# Get the prob-weighted loose run's per-day net to identify losers
import subprocess
PY = sys.executable
subprocess.run([PY, 'scripts/backtest_strategy.py', '--strategy', 'wing',
                '--wing-assumed-win-prob', '0.99', '--wing-max-ask-sum', '1.00',
                '--wing-sizing-mode', 'prob_weighted',
                '--out', 'data/anatomy.parquet'], capture_output=True, cwd=_ROOT)
positions = pd.read_parquet(_ROOT / 'data/anatomy_positions.parquet')
day_pnl = positions.groupby('date')['net_cents'].sum().to_dict()
losing_days = sorted([d for d, n in day_pnl.items() if n < 0])

print(f"Found {len(losing_days)} losing days under prob-weighted (loose filter)")
print()

# For each losing day, full anatomy
for ld in losing_days:
    date = pd.Timestamp(ld).normalize()
    # find OOF row
    idx = feat_v.index[feat_v['date'] == date]
    if len(idx) == 0:
        print(f"=== {date.date()}: no OOF row ===\n"); continue
    i = idx[0]
    actual = truth.get(date)
    if actual is None:
        print(f"=== {date.date()}: no truth ===\n"); continue

    # Build all contracts
    d_kalshi = kalshi[kalshi['event_date'] == date]
    anchor_t = (date.tz_localize('America/Chicago') + pd.Timedelta(hours=13)).tz_convert('UTC')
    pre = d_kalshi[d_kalshi['timestamp'] <= anchor_t]
    latest = pre.sort_values('timestamp').groupby('ticker', as_index=False).last()

    contracts = []
    for _, row in latest.iterrows():
        spec = parse_kalshi_subtitle(row.get('subtitle'))
        if spec is None: continue
        yes_ask = min((row['yes_price_cents'] or 50) / 100 + 0.01, 0.99)
        p = bucket_prob(oof_v[i], spec)
        contracts.append({'spec': spec, 'yes_ask': yes_ask, 'p': p,
                          'pos_lo': bucket_lower_bound(spec),
                          'ticker': row['ticker']})
    contracts.sort(key=lambda c: c['pos_lo'])

    # Identify modal + wing
    model_modal_idx = max(range(len(contracts)), key=lambda j: contracts[j]['p'])
    market_modal_idx = max(range(len(contracts)), key=lambda j: contracts[j]['yes_ask'])
    agreement = (contracts[model_modal_idx]['spec'] == contracts[market_modal_idx]['spec'])
    wing_indices = [model_modal_idx]
    if model_modal_idx > 0: wing_indices.append(model_modal_idx - 1)
    if model_modal_idx < len(contracts) - 1: wing_indices.append(model_modal_idx + 1)

    # Get the actual trades from positions
    day_positions = positions[positions['date'] == ld]

    # Find which contract was the winner (per CLI truth)
    winner_idx = None
    for j, c in enumerate(contracts):
        pred, _ = parse_bucket(c['spec'])
        if pred(actual):
            winner_idx = j; break

    net = day_pnl[ld]
    pnl_dollars = net / 100.0

    print(f"=== {date.date()}: actual = {actual}F, net = ${pnl_dollars:+.2f} ===")
    print(f"  Agreement: {agreement}; model_modal={contracts[model_modal_idx]['spec']}; market_modal={contracts[market_modal_idx]['spec']}")
    if winner_idx is not None:
        winner_role = 'modal' if winner_idx == model_modal_idx else ('wing' if winner_idx in wing_indices else 'OUTSIDE WING')
        print(f"  Winner: {contracts[winner_idx]['spec']} ({winner_role}, p_model={contracts[winner_idx]['p']:.3f})")
    else:
        print(f"  Winner: nothing matched (truth outside all 6 buckets — shouldn't happen)")

    # Render the bucket layout
    print()
    print(f"  {'bucket':<10} {'role':<10} {'p_model':>8} {'yes_ask':>8} {'contracts':>10} {'stake$':>8} {'payout':>8} {'net$':>8}")
    print(f"  {'-'*78}")
    for j, c in enumerate(contracts):
        role_bits = []
        if j == model_modal_idx: role_bits.append('MODAL')
        if j == market_modal_idx and j != model_modal_idx: role_bits.append('MKT-MOD')
        if j in wing_indices and j != model_modal_idx: role_bits.append('wing')
        if j == winner_idx: role_bits.append('WIN!')
        role = ','.join(role_bits)

        # Find this contract's leg in positions
        leg = day_positions[day_positions['ticker'] == c['ticker']]
        if len(leg):
            r = leg.iloc[0]
            stake = r['stake_cents'] / 100
            n_contracts = r['contracts']
            payout = n_contracts if r['won'] else 0
            net_leg = r['net_cents'] / 100
            print(f"  {c['spec']:<10} {role:<10} {c['p']:>8.3f} {c['yes_ask']:>8.2f} {n_contracts:>10} {stake:>8.2f} {payout:>8} {net_leg:>+8.2f}")
        else:
            print(f"  {c['spec']:<10} {'(skip)':<10} {c['p']:>8.3f} {c['yes_ask']:>8.2f} {'—':>10} {'—':>8} {'—':>8} {'—':>8}")
    print()
    # Diagnose
    if winner_idx is None:
        why = "no Kalshi bucket matched actual temperature (data issue)"
    elif winner_idx == model_modal_idx:
        why = "modal won but prob-weighted gave it fewer contracts than equal-payout would; net win didn't cover the 2 wing legs that lost"
    elif winner_idx in wing_indices:
        wing_winner = contracts[winner_idx]
        modal_p = contracts[model_modal_idx]['p']
        if wing_winner['p'] < modal_p / 2:
            why = (f"truth was at LOW-PROB wing leg (p={wing_winner['p']:.3f} vs modal {modal_p:.3f}); "
                   "prob-weighted under-allocated to this leg, so the winning payout was too small to cover modal+other losses")
        else:
            why = f"wing leg won (p={wing_winner['p']:.3f}) but with smaller-than-equal-payout allocation, win didn't fully cover other 2 losses"
    else:
        why = "truth was OUTSIDE the wing — both adjacents and modal lost; the full-stake-loss case"
    print(f"  WHY THIS LOST: {why}")
    print()
