"""Compare the v3 (1 PM anchor) and v4 (midnight anchor) models against the
Kalshi market at bucket resolution.

Reuses the methodology from scripts/verify_bucket_accuracy.py, parameterized
over (model artifact dir, market-snapshot anchor hour). For each model it reports:

  A) §10.2-style fixed 2°F-pair bucket accuracy (MODEL-ONLY, full OOF window).
     This is the model's standalone bucketing skill, independent of Kalshi.

  B) Kalshi-resolution accuracy: model-modal vs market-modal Top-1/Top-3, split
     by agreement (model_modal == market_modal), on the Kalshi window — at TWO
     market-snapshot anchors:
       * midnight (00:00 local)  -> equal-information / fair skill test for v4
       * 1 PM     (13:00 local)  -> the v3 §10.3 frame (and v3's deployed anchor)

Run:  python scripts/v3_v4_kalshi_compare.py
Requires data/model_v3_artifacts/ and data/model_v4_artifacts/ to exist.
"""
from __future__ import annotations
import sys
from pathlib import Path
import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from forecast_alpha.pmf import parse_bucket, parse_kalshi_subtitle, bucket_prob

try:  # Windows consoles default to cp1252; the report uses §/°/→/±.
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

F_GRID = np.arange(-30, 131)   # 161 integer-°F bins, shared by v3 and v4


# ---------------------------------------------------------------------------
# Loaders / metrics
# ---------------------------------------------------------------------------
def load_model(artifact_dir: str):
    """Return (calibrated OOF PMFs, actual highs, prediction dates) for valid rows."""
    d = _ROOT / "data" / artifact_dir
    oof = np.load(d / "oof_bucket_probs_calib.npy")
    fold = np.load(d / "oof_fold.npy")
    feat = pd.read_parquet(d / "feature_df.parquet").reset_index(drop=True)
    tgt = pd.read_parquet(d / "target_df.parquet").reset_index(drop=True)

    valid = (fold >= 0) & ~np.isnan(oof).any(axis=1) & ~tgt["cli_high"].isna()
    probs = oof[valid]
    actual = tgt.loc[valid, "cli_high"].astype(int).values
    dates = pd.to_datetime(feat.loc[valid, "date"]).dt.normalize()
    assert probs.shape[1] == len(F_GRID), f"{artifact_dir}: PMF width {probs.shape[1]} != 161"
    return probs, actual, dates.reset_index(drop=True)


def fixed_2f_bucket_acc(probs: np.ndarray, actual: np.ndarray):
    """§10.2 model-only accuracy on fixed 2°F pairs (low-even, high-odd)."""
    grid_lo = -30 - (-30 % 2)
    bucket_for_F = (F_GRID - grid_lo) // 2
    n_buckets = bucket_for_F.max() + 1
    agg = np.zeros((probs.shape[0], n_buckets))
    np.add.at(agg.T, bucket_for_F, probs.T)
    model_argmax = agg.argmax(axis=1)
    actual_bucket = (actual - grid_lo) // 2
    top1 = (model_argmax == actual_bucket).mean()
    top3 = (np.abs(model_argmax - actual_bucket) <= 1).mean()
    return top1, top3, len(probs)


def _spec_lo(spec: str) -> int:
    if spec.startswith("<="):
        return int(spec[2:]) - 100
    if spec.startswith(">="):
        return int(spec[2:])
    return int(spec.split("-")[0])


def kalshi_records(probs, dates, kalshi, truth, anchor_hour: int) -> pd.DataFrame:
    """One row per Kalshi day with model-modal, market-modal, and truth bucket
    indices, using the market snapshot at `anchor_hour` local."""
    date_to_idx = {d: i for i, d in enumerate(dates)}
    records = []
    for ev_date in sorted(kalshi["event_date"].unique()):
        ev = pd.Timestamp(ev_date).normalize()
        if ev not in date_to_idx or ev not in truth:
            continue
        actual_f = truth[ev]
        anchor_t = (ev.tz_localize("America/Chicago")
                    + pd.Timedelta(hours=anchor_hour)).tz_convert("UTC")
        pre = kalshi[(kalshi["event_date"] == ev) & (kalshi["timestamp"] <= anchor_t)]
        if pre.empty:
            continue
        latest = pre.sort_values("timestamp").groupby("ticker", as_index=False).last()

        pmf = probs[date_to_idx[ev]]
        contracts = []
        for _, row in latest.iterrows():
            spec = parse_kalshi_subtitle(row.get("subtitle"))
            if spec is None:
                continue
            yes_ask = min((row["yes_price_cents"] or 50) / 100 + 0.01, 0.99)
            contracts.append({"spec": spec, "yes_ask": yes_ask,
                              "p_model": bucket_prob(pmf, spec)})
        if len(contracts) < 2:
            continue
        contracts.sort(key=lambda c: _spec_lo(c["spec"]))

        model_mod = max(range(len(contracts)), key=lambda j: contracts[j]["p_model"])
        market_mod = max(range(len(contracts)), key=lambda j: contracts[j]["yes_ask"])
        actual_idx = None
        for j, c in enumerate(contracts):
            pred, _ = parse_bucket(c["spec"])
            if pred(actual_f):
                actual_idx = j
                break
        if actual_idx is None:
            continue

        records.append({
            "date": ev, "actual_f": actual_f, "actual_idx": actual_idx,
            "model_mod": model_mod, "market_mod": market_mod,
            "agreement": model_mod == market_mod, "n_contracts": len(contracts),
            "model_spec": contracts[model_mod]["spec"],
            "market_spec": contracts[market_mod]["spec"],
            "actual_spec": contracts[actual_idx]["spec"],
        })
    return pd.DataFrame(records)


def _acc(df):
    if len(df) == 0:
        return None
    return {
        "n": len(df),
        "model_top1": (df["actual_idx"] == df["model_mod"]).mean(),
        "market_top1": (df["actual_idx"] == df["market_mod"]).mean(),
        "model_top3": (np.abs(df["actual_idx"] - df["model_mod"]) <= 1).mean(),
        "market_top3": (np.abs(df["actual_idx"] - df["market_mod"]) <= 1).mean(),
    }


def print_kalshi_block(df: pd.DataFrame, label: str):
    print(f"\n  {label}")
    if len(df) == 0:
        print("    (no usable Kalshi days at this anchor)")
        return
    print(f"    Days: {len(df)}   agreement (model==market modal): "
          f"{df['agreement'].sum()} ({df['agreement'].mean():.1%})")
    rows = [("ALL days", df),
            ("Agreement", df[df["agreement"]]),
            ("Disagreement", df[~df["agreement"]])]
    print(f"    {'subset':<14}{'n':>4}  {'model T1':>9}{'mkt T1':>8}   {'model T3':>9}{'mkt T3':>8}")
    for name, sub in rows:
        a = _acc(sub)
        if a is None:
            print(f"    {name:<14}{0:>4}")
            continue
        print(f"    {name:<14}{a['n']:>4}  {a['model_top1']:>8.1%}{a['market_top1']:>8.1%}   "
              f"{a['model_top3']:>8.1%}{a['market_top3']:>8.1%}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    kalshi = pd.read_parquet(_ROOT / "data/kalshi_history.parquet")
    kalshi["event_date"] = pd.to_datetime(kalshi["event_date"]).dt.normalize()
    kalshi["timestamp"] = pd.to_datetime(kalshi["timestamp"], utc=True)

    cli = pd.read_parquet(_ROOT / "data/cli_KMDW.parquet")
    cli["date"] = pd.to_datetime(cli["date"]).dt.normalize()
    truth = {d: int(t) for d, t in
             cli[["date", "max_temp_f"]].dropna().itertuples(index=False, name=None)}

    models = [
        ("v3 (1 PM anchor, PRODUCTION)", "model_v3_artifacts"),
        ("v4 (midnight anchor, R&D)", "model_v4_artifacts"),
    ]
    anchors = [(0, "market @ midnight (00:00 local) — equal-info / fair"),
               (13, "market @ 1 PM (13:00 local) — v3 §10.3 frame")]

    for label, art in models:
        d = _ROOT / "data" / art
        print("=" * 78)
        print(label)
        print("=" * 78)
        if not (d / "oof_bucket_probs_calib.npy").exists():
            print(f"  MISSING artifacts at {d} — skipping.")
            continue
        probs, actual, dates = load_model(art)
        t1, t3, n = fixed_2f_bucket_acc(probs, actual)
        print(f"  §10.2 model-only fixed-2°F bucket accuracy "
              f"({n} OOF days, {dates.min().date()} → {dates.max().date()}):")
        print(f"    Top-1 (argmax == truth bucket): {t1:.1%}")
        print(f"    Top-3 (truth within ±1 bucket): {t3:.1%}")
        for ah, alabel in anchors:
            df = kalshi_records(probs, dates, kalshi, truth, ah)
            print_kalshi_block(df, alabel)
            # date-alignment sanity: show 3 example rows the first time
            if ah == 13 and len(df) > 0:
                ex = df.head(3)[["date", "actual_f", "model_spec", "market_spec", "actual_spec"]]
                print("    sanity (first 3 days):")
                for _, r in ex.iterrows():
                    print(f"      {r['date'].date()}  truth={r['actual_f']}°F  "
                          f"model={r['model_spec']:<7} market={r['market_spec']:<7} truth_bkt={r['actual_spec']}")
        print()


if __name__ == "__main__":
    main()
