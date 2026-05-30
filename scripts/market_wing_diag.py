"""Diagnose market_wing+drop_lower_ask losses and run the agreement-required variant.

Three tasks:
  1. Anatomy of each loss day in market_wing+drop_lower_ask
  2. Run market_wing+drop_lower_ask WITH agreement filter (should == wing+drop_lower_ask)
  3. Compare configurations side-by-side
"""

from __future__ import annotations
import subprocess, sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from weather_alpha.pmf import parse_bucket, parse_kalshi_subtitle, bucket_prob

PY = sys.executable
BACKTEST = "scripts/backtest_strategy.py"


def run(tag, args):
    out = f"data/mw2_{tag}.parquet"
    subprocess.run([PY, BACKTEST] + args + ["--out", out], capture_output=True, cwd=_ROOT)
    daily = pd.read_parquet(_ROOT / out)
    pos = pd.read_parquet(_ROOT / out.replace(".parquet", "_positions.parquet"))
    return daily, pos


# (1) Anatomy of losing days under market_wing+drop_lower_ask
print("=" * 78)
print("(1) Loss-day anatomy: market_wing + drop_lower_ask")
print("=" * 78)
pos = pd.read_parquet(_ROOT / "data/mw_droplow_positions.parquet")
pos["date"] = pd.to_datetime(pos["date"]).dt.normalize()
day = pos.groupby("date").agg(net=("net_cents", "sum")).reset_index().sort_values("date")
loss_days = day[day["net"] < 0]["date"].tolist()
print(f"Loss days: {len(loss_days)}\n")

oof = np.load(_ROOT / "data/model_v3_artifacts/oof_bucket_probs_calib.npy")
fold = np.load(_ROOT / "data/model_v3_artifacts/oof_fold.npy")
feat = pd.read_parquet(_ROOT / "data/model_v3_artifacts/feature_df.parquet").reset_index(drop=True)
valid = (fold >= 0) & ~np.isnan(oof).any(axis=1)
oof_v = oof[valid]; feat_v = feat.loc[valid].reset_index(drop=True)
date_to_i = {pd.Timestamp(d).normalize(): i for i, d in enumerate(feat_v["date"])}
kalshi = pd.read_parquet(_ROOT / "data/kalshi_history.parquet")
kalshi["event_date"] = pd.to_datetime(kalshi["event_date"]).dt.normalize()
cli = pd.read_parquet(_ROOT / "data/cli_KMDW.parquet")
cli["date"] = pd.to_datetime(cli["date"]).dt.normalize()
truth = {d: int(t) for d, t in cli[["date", "max_temp_f"]].dropna().itertuples(index=False, name=None)}


def _lo(spec):
    if spec.startswith("<="): return int(spec[2:]) - 100
    if spec.startswith(">="): return int(spec[2:])
    return int(spec.split("-")[0])


for ld in loss_days:
    if ld not in date_to_i or ld not in truth:
        continue
    actual = truth[ld]
    anchor = (ld.tz_localize("America/Chicago") + pd.Timedelta(hours=13)).tz_convert("UTC")
    pre = kalshi[(kalshi["event_date"] == ld) & (kalshi["timestamp"] <= anchor)]
    latest = pre.sort_values("timestamp").groupby("ticker", as_index=False).last()
    pmf = oof_v[date_to_i[ld]]

    bks = []
    for _, r in latest.iterrows():
        spec = parse_kalshi_subtitle(r.get("subtitle"))
        if spec is None: continue
        yes_ask = min((r["yes_price_cents"] or 50)/100 + 0.01, 0.99)
        p_model = bucket_prob(pmf, spec)
        bks.append({"spec": spec, "yes_ask": yes_ask, "p_model": p_model, "ticker": r["ticker"]})
    bks.sort(key=lambda c: _lo(c["spec"]))
    model_i = max(range(len(bks)), key=lambda j: bks[j]["p_model"])
    market_i = max(range(len(bks)), key=lambda j: bks[j]["yes_ask"])
    # Wing centered on market modal (with drop_lower_ask -> 2 legs)
    adjs = [j for j in range(len(bks)) if j == market_i - 1 or j == market_i + 1]
    if not adjs: continue
    higher_adj_i = max(adjs, key=lambda j: bks[j]["yes_ask"])
    lower_adj_i = min(adjs, key=lambda j: bks[j]["yes_ask"]) if len(adjs) > 1 else None
    bought = [market_i, higher_adj_i]
    truth_i = next((j for j, c in enumerate(bks) if (lambda s: (
        int(s[2:]) >= actual if s.startswith("<=") else
        int(s[2:]) <= actual if s.startswith(">=") else
        int(s.split("-")[0]) <= actual <= int(s.split("-")[1])
    ))(c["spec"])), None)

    agreement = model_i == market_i
    net = day[day["date"] == ld]["net"].iloc[0] / 100
    inside_2leg = truth_i in bought if truth_i is not None else False
    inside_3leg = truth_i in (bought + [j for j in adjs if j != higher_adj_i]) if truth_i is not None else False

    truth_role = ("BOUGHT-modal" if truth_i == market_i else
                  "BOUGHT-higher_adj" if truth_i == higher_adj_i else
                  "DROPPED-lower_adj" if truth_i == lower_adj_i else
                  "OUTSIDE wing")
    print(f"{ld.date()}  actual={actual}F  net=${net:+.2f}  agree={agreement}")
    print(f"   market_modal={bks[market_i]['spec']} (ask {bks[market_i]['yes_ask']:.2f})  "
          f"model_modal={bks[model_i]['spec']} (p {bks[model_i]['p_model']:.2f})")
    print(f"   bought: {[bks[j]['spec'] for j in bought]}  "
          f"truth bucket: {bks[truth_i]['spec'] if truth_i is not None else 'NONE'} "
          f"({truth_role})")

# (2) Run market_wing+drop_lower_ask WITH agreement requirement
print()
print("=" * 78)
print("(2) market_wing + drop_lower_ask + REQUIRE AGREEMENT")
print("    (uses --strategy wing --wing-anchor market)")
print("=" * 78)
_, pos_ma = run("market_agree", ["--strategy", "wing", "--wing-anchor", "market",
                                  "--wing-drop-lower-ask",
                                  "--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "1.00"])
_, pos_model = run("model_agree", ["--strategy", "wing",
                                    "--wing-drop-lower-ask",
                                    "--wing-assumed-win-prob", "0.99", "--wing-max-ask-sum", "1.00"])

def summary(label, pos):
    if pos.empty:
        print(f"{label}: no trades"); return
    d = pos.groupby("date").agg(net=("net_cents", "sum")).reset_index().sort_values("date")
    n = len(d); W = (d["net"] > 0).sum(); L = (d["net"] < 0).sum()
    pnl = d["net"].sum() / 100
    worst = d["net"].min() / 100
    print(f"{label:<55}  days={n}  W/L={W}/{L}  PnL=${pnl:+.2f}  worst=${worst:+.2f}")
    return d

d1 = summary("market_wing + drop_lower_ask + agreement", pos_ma)
d2 = summary("wing + drop_lower_ask (model anchor + agreement)", pos_model)

# (3) Confirm they're identical
print()
print("=" * 78)
print("(3) Trade-by-trade comparison: market-anchor+agree  vs  model-anchor+agree")
print("=" * 78)
joined = d1.merge(d2, on="date", how="outer", suffixes=("_market", "_model"))
joined["delta"] = (joined["net_market"].fillna(0) - joined["net_model"].fillna(0)) / 100
print(f"{'date':<12} {'market$':>10} {'model$':>10} {'delta$':>9}")
for _, r in joined.iterrows():
    m1 = r["net_market"]/100 if pd.notna(r["net_market"]) else None
    m2 = r["net_model"]/100 if pd.notna(r["net_model"]) else None
    m1s = f"{m1:+.2f}" if m1 is not None else "--"
    m2s = f"{m2:+.2f}" if m2 is not None else "--"
    print(f"{r['date'].date()} {m1s:>10} {m2s:>10} {r['delta']:>+9.2f}")
print()
print(f"Max abs delta: ${joined['delta'].abs().max():.2f}")
