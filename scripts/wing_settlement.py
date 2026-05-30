"""Wing settlement % at a chosen market-snapshot anchor, for the v3 and v4
models and for the market itself.

For each Kalshi day, build the coverage wing around an anchor (model_modal or
market_modal) using the market snapshot at the given local hour, and report how
often settlement lands inside it, for three widths:

  - 1-leg : anchor bucket only            (= Top-1 / "1st modal")
  - 2-leg : anchor + higher-ask adjacent  (= production drop_lower_ask / "+2nd modal")
  - 3-leg : anchor + both adjacents       (= Top-3, truth within ±1)

Model and market rows for a given model share the SAME day set, so they are
directly comparable. On agreement days (model modal == market modal) the
model- and market-anchored wings are identical by construction.

Usage:
    python scripts/wing_settlement.py            # 1 PM (13:00)
    python scripts/wing_settlement.py 0 13       # midnight and 1 PM
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
from weather_alpha.pmf import parse_bucket, parse_kalshi_subtitle, bucket_prob

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

F_GRID = np.arange(-30, 131)


def load_model(art):
    d = _ROOT / "data" / art
    oof = np.load(d / "oof_bucket_probs_calib.npy")
    fold = np.load(d / "oof_fold.npy")
    feat = pd.read_parquet(d / "feature_df.parquet").reset_index(drop=True)
    tgt = pd.read_parquet(d / "target_df.parquet").reset_index(drop=True)
    valid = (fold >= 0) & ~np.isnan(oof).any(axis=1) & ~tgt["cli_high"].isna()
    dates = pd.to_datetime(feat.loc[valid, "date"]).dt.normalize().reset_index(drop=True)
    return oof[valid], dates


def _lo(spec):
    if spec.startswith("<="):
        return int(spec[2:]) - 100
    if spec.startswith(">="):
        return int(spec[2:])
    return int(spec.split("-")[0])


def day_contracts(kalshi, ev, anchor_hour):
    anchor_t = (ev.tz_localize("America/Chicago")
                + pd.Timedelta(hours=anchor_hour)).tz_convert("UTC")
    pre = kalshi[(kalshi["event_date"] == ev) & (kalshi["timestamp"] <= anchor_t)]
    if pre.empty:
        return None
    latest = pre.sort_values("timestamp").groupby("ticker", as_index=False).last()
    rows = []
    for _, row in latest.iterrows():
        spec = parse_kalshi_subtitle(row.get("subtitle"))
        if spec is None:
            continue
        rows.append({"spec": spec,
                     "ask": min((row["yes_price_cents"] or 50) / 100 + 0.01, 0.99)})
    if len(rows) < 2:
        return None
    rows.sort(key=lambda c: _lo(c["spec"]))
    return rows


def truth_idx(contracts, actual_f):
    for j, c in enumerate(contracts):
        pred, _ = parse_bucket(c["spec"])
        if pred(actual_f):
            return j
    return None


def wings(contracts, scores):
    """(anchor_idx, {1-leg}, {2-leg drop_lower_ask}, {3-leg}) given per-leg scores."""
    n = len(contracts)
    a = max(range(n), key=lambda j: scores[j])
    adj = [j for j in (a - 1, a + 1) if 0 <= j < n]
    one, three = {a}, {a, *adj}
    two = {a, max(adj, key=lambda j: contracts[j]["ask"])} if len(adj) >= 2 else {a, *adj}
    return a, one, two, three


def main():
    hours = [int(h) for h in sys.argv[1:]] or [13]
    kalshi = pd.read_parquet(_ROOT / "data/kalshi_history.parquet")
    kalshi["event_date"] = pd.to_datetime(kalshi["event_date"]).dt.normalize()
    kalshi["timestamp"] = pd.to_datetime(kalshi["timestamp"], utc=True)
    cli = pd.read_parquet(_ROOT / "data/cli_KMDW.parquet")
    cli["date"] = pd.to_datetime(cli["date"]).dt.normalize()
    truth = {d: int(t) for d, t in
             cli[["date", "max_temp_f"]].dropna().itertuples(index=False, name=None)}

    models = {tag: load_model(art) for tag, art in
              [("v3", "model_v3_artifacts"), ("v4", "model_v4_artifacts")]}
    ev_dates = sorted(pd.Timestamp(d).normalize() for d in kalshi["event_date"].unique())

    def hdr():
        print(f"    {'anchor':<11}{'1-leg modal':>13}{'2-leg modal+2nd':>18}{'3-leg ±1':>11}")

    def line(name, sub, p):
        print(f"    {name:<11}{sub[f'{p}1'].mean():>12.1%}"
              f"{sub[f'{p}2'].mean():>18.1%}{sub[f'{p}3'].mean():>11.1%}")

    for H in hours:
        label = {0: "midnight (00:00 local)", 13: "1 PM (13:00 local)"}.get(H, f"{H}:00 local")
        print("=" * 66)
        print(f"WING SETTLEMENT — market snapshot @ {label}")
        print("=" * 66)
        for tag in ("v3", "v4"):
            oof, dates = models[tag]
            idx = {d: i for i, d in enumerate(dates)}
            recs = []
            for ev in ev_dates:
                if ev not in truth or ev not in idx:
                    continue
                c = day_contracts(kalshi, ev, H)
                if c is None:
                    continue
                ti = truth_idx(c, truth[ev])
                if ti is None:
                    continue
                pmf = oof[idx[ev]]
                pm = [bucket_prob(pmf, x["spec"]) for x in c]
                asks = [x["ask"] for x in c]
                ma, o1, o2, o3 = wings(c, pm)
                ka, k1, k2, k3 = wings(c, asks)
                recs.append({"agree": ma == ka,
                             "mdl_1": ti in o1, "mdl_2": ti in o2, "mdl_3": ti in o3,
                             "mkt_1": ti in k1, "mkt_2": ti in k2, "mkt_3": ti in k3})
            df = pd.DataFrame(recs)
            print(f"\n  {tag} day set — ALL days (n={len(df)}), "
                  f"agreement {df.agree.mean():.0%} ({int(df.agree.sum())}/{len(df)})")
            hdr()
            line(f"{tag} model", df, "mdl_")
            line("market", df, "mkt_")
            ag = df[df.agree]
            if len(ag):
                print(f"    -- agreement days only (n={len(ag)}; wing == market wing):")
                line(f"{tag} model", ag, "mdl_")
        print()


if __name__ == "__main__":
    main()
