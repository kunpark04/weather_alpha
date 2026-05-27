"""Walk-forward backtest of the production strategy on real Kalshi history.

Look-ahead-bias prevention (see docstrings inside main()):
  - Model PMF: OOF walk-forward predictions from §5 (data/model_v3_artifacts/
    oof_bucket_probs_calib.npy). Rows with fold == -1 (warmup) are dropped.
  - Features: feature_df.parquet snapshot — built with leakage-safe lags
    (cli_high_yesterday, lag_mean_3d, rolling 5yr with current-year shift).
  - Kalshi quotes: last trade ON OR BEFORE the anchor moment (1 PM CDT/CST)
    on each date. NEVER post-anchor / post-settlement prices.
  - CLI truth: used only for settlement, never as a feature.
  - Strategy / risk knobs: held at the values in config/forecast_alpha.yaml.
    Don't tune them on this backtest's window — see CAVEATS in the printout.

Usage:
    python scripts/backtest_strategy.py
    python scripts/backtest_strategy.py --assumed-spread-cents 2
    python scripts/backtest_strategy.py --start 2024-01-01 --end 2026-05-21
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from forecast_alpha.config import load_config
from forecast_alpha.fees import trade_fee_cents
from forecast_alpha.kalshi import KalshiContract
from forecast_alpha.model import Prediction, load_artifacts
from forecast_alpha.pmf import INTEGER_F_GRID, parse_bucket, parse_kalshi_subtitle
from forecast_alpha.strategy import run_strategy


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_oof_dataset(model_dir: Path) -> dict:
    """Load OOF predictions + features. Drop rows where fold == -1 (warmup) or
    PMF contains NaN. Result is bias-free, in chronological order."""
    oof_calib = np.load(model_dir / "oof_bucket_probs_calib.npy")
    oof_fold = np.load(model_dir / "oof_fold.npy")
    feat_df = pd.read_parquet(model_dir / "feature_df.parquet").reset_index(drop=True)
    tgt_df = pd.read_parquet(model_dir / "target_df.parquet").reset_index(drop=True)

    valid = (oof_fold >= 0) & ~np.isnan(oof_calib).any(axis=1)
    print(f"OOF dataset: {valid.sum()} usable rows / {len(valid)} total "
          f"(dropped {(~valid).sum()}: {(oof_fold == -1).sum()} warmup, "
          f"{np.isnan(oof_calib).any(axis=1).sum()} NaN PMFs)")

    return {
        "pmf":         oof_calib[valid],
        "fold":        oof_fold[valid],
        "feature_df":  feat_df.loc[valid].reset_index(drop=True),
        "target_df":   tgt_df.loc[valid].reset_index(drop=True),
    }


def load_kalshi_history(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["event_date"] = pd.to_datetime(df["event_date"], utc=True).dt.tz_localize(None).dt.normalize()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return df


# ---------------------------------------------------------------------------
# Market snapshot — last trade at-or-before anchor_t_utc, per ticker
# ---------------------------------------------------------------------------

def snapshot_at_anchor(kalshi: pd.DataFrame, anchor_date: pd.Timestamp,
                       anchor_t_utc: pd.Timestamp,
                       assumed_spread_cents: int = 2) -> list[KalshiContract]:
    """For each Kalshi ticker for this event_date, take the last trade before anchor_t_utc."""
    event_mask = (kalshi["event_date"] == anchor_date) & (kalshi["timestamp"] <= anchor_t_utc)
    pre_anchor = kalshi.loc[event_mask]
    if pre_anchor.empty:
        return []

    # Latest trade per ticker
    latest = (pre_anchor.sort_values("timestamp")
              .groupby("ticker", as_index=False).last())

    contracts: list[KalshiContract] = []
    half_spread = assumed_spread_cents / 200.0  # in dollars, applied as ±

    for _, row in latest.iterrows():
        spec = parse_kalshi_subtitle(row.get("subtitle"))
        if spec is None:
            st = row.get("strike_type")
            fs = row.get("floor_strike")
            if st == "less" and pd.notna(fs):
                spec = f"<{int(fs)}"
            elif st == "greater" and pd.notna(fs):
                spec = f">{int(fs)}"
            else:
                continue

        yes_mid = (row["yes_price_cents"] or 50) / 100
        no_mid = (row["no_price_cents"] or 50) / 100
        yes_ask = min(yes_mid + half_spread, 0.99)
        yes_bid = max(yes_mid - half_spread, 0.01)
        no_ask = min(no_mid + half_spread, 0.99)
        no_bid = max(no_mid - half_spread, 0.01)

        contracts.append(KalshiContract(
            ticker=row["ticker"], event_ticker=row["event_ticker"], bucket_spec=spec,
            subtitle=row.get("subtitle") or "",
            strike_type=row.get("strike_type"), strike=row.get("floor_strike"),
            yes_bid=yes_bid, yes_ask=yes_ask, no_bid=no_bid, no_ask=no_ask,
            last_price=yes_mid, volume_24h=None, open_interest=None,
            open_time_utc=None, close_time_utc=None,
            status="active", is_live=True,
        ))
    return contracts


# ---------------------------------------------------------------------------
# Settlement
# ---------------------------------------------------------------------------

def settle_position(bucket_spec: str, actual_f: int, side: str) -> bool:
    pred, _ = parse_bucket(bucket_spec)
    bucket_resolves_yes = pred(int(actual_f))
    return (side == "yes" and bucket_resolves_yes) or (side == "no" and not bucket_resolves_yes)


# ---------------------------------------------------------------------------
# Walk-forward loop
# ---------------------------------------------------------------------------

def backtest(cfg, oof, kalshi, cli, start, end, assumed_spread_cents,
             starting_bankroll: float | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    art = load_artifacts(cfg.paths.model_dir)
    cli_truth = {pd.Timestamp(d).normalize(): int(v)
                 for d, v in cli[["date", "max_temp_f"]].dropna().itertuples(index=False, name=None)}

    bankroll = starting_bankroll or cfg.strategy.bankroll_usd
    cum_pnl_cents = 0
    rows: list[dict] = []
    positions: list[dict] = []
    n_predicted = 0
    n_traded = 0

    for i in range(len(oof["pmf"])):
        date = pd.Timestamp(oof["feature_df"]["date"].iloc[i]).normalize()
        if (start and date < start) or (end and date > end):
            continue
        n_predicted += 1

        anchor_t_local = date.tz_localize(cfg.local_tz) + pd.Timedelta(hours=cfg.model.anchor_hour_local)
        anchor_t_utc = anchor_t_local.tz_convert("UTC")
        contracts = snapshot_at_anchor(kalshi, date, anchor_t_utc, assumed_spread_cents)
        if not contracts:
            continue

        # Reconstruct Prediction from OOF PMF
        pmf_values = oof["pmf"][i]
        cdf = np.cumsum(pmf_values)
        pmf_series = pd.Series(pmf_values, index=art.integer_f_grid, name="P")
        pred = Prediction(
            date=date, model=art.name, pmf=pmf_series,
            median=int(art.integer_f_grid[np.searchsorted(cdf, 0.50)]),
            lo10=int(art.integer_f_grid[np.searchsorted(cdf, 0.10)]),
            hi90=int(art.integer_f_grid[np.searchsorted(cdf, 0.90)]),
            peak_F=int(pmf_series.idxmax()), peak_P=float(pmf_series.max()),
            season=art.season_of_month[date.month],
            hard_floor=None, leaked_below_floor=0.0, nan_features=[],
        )

        feature_row = oof["feature_df"].iloc[i]
        strat_out = run_strategy(cfg.strategy, pred, contracts, feature_row, bankroll)

        if date not in cli_truth:
            continue
        actual_f = cli_truth[date]

        day_pnl_cents = 0
        day_fees_cents = 0
        day_stake_cents = 0
        for tgt in strat_out.targets:
            c = next((x for x in contracts if x.ticker == tgt.ticker), None)
            if c is None:
                continue
            price = c.yes_ask if tgt.side == "yes" else c.no_ask
            n = tgt.target_contracts
            cost_cents = int(round(price * 100)) * n
            fee_cents = trade_fee_cents(price, n)
            won = settle_position(tgt.bucket_spec, actual_f, tgt.side)
            payout_cents = n * 100 if won else 0
            net = payout_cents - cost_cents - fee_cents
            day_pnl_cents += net
            day_fees_cents += fee_cents
            day_stake_cents += cost_cents
            positions.append({
                "date":          date,
                "actual_f":      actual_f,
                "ticker":        tgt.ticker,
                "bucket_spec":   tgt.bucket_spec,
                "side":          tgt.side,
                "contracts":     n,
                "entry_cents":   int(round(price * 100)),
                "stake_cents":   cost_cents,
                "fee_cents":     fee_cents,
                "won":           won,
                "net_cents":     net,
                "p_model":       float(tgt.rationale.get("p_model", 0)),
                "p_market":      float(tgt.rationale.get("p_market", 0)),
                "kelly_f":       float(tgt.rationale.get("kelly_f", 0)),
                "net_ev_cents":  float(tgt.rationale.get("net_ev_cents", 0)),
                "bankroll_pre":  bankroll,
            })

        if strat_out.targets:
            n_traded += 1
        cum_pnl_cents += day_pnl_cents
        bankroll = max(0.0, (cfg.strategy.bankroll_usd or 0) + cum_pnl_cents / 100.0)

        rows.append({
            "date":             date,
            "actual_f":         actual_f,
            "median_pred":      pred.median,
            "peak_pred":        pred.peak_F,
            "peak_P":           pred.peak_P,
            "season":           pred.season,
            "n_live_contracts": len(contracts),
            "n_targets":        len(strat_out.targets),
            "day_stake_cents":  day_stake_cents,
            "day_fees_cents":   day_fees_cents,
            "day_pnl_cents":    day_pnl_cents,
            "cum_pnl_cents":    cum_pnl_cents,
            "bankroll_usd":     bankroll,
            "throttle":         strat_out.diagnostics.get("throttle", 1.0),
            "kl_total":         strat_out.diagnostics.get("kl_total", None),
            "abs_err_F":        abs(pred.median - actual_f),
            "in_80pct_ci":      pred.lo10 <= actual_f <= pred.hi90,
        })

    print(f"Walked {n_predicted} OOF predictions; {n_traded} had a trade; "
          f"{len(rows)} settled rows; {len(positions)} positions")
    return pd.DataFrame(rows), pd.DataFrame(positions)


# ---------------------------------------------------------------------------
# Summary statistics
# ---------------------------------------------------------------------------

def print_summary(df: pd.DataFrame, cfg) -> None:
    if df.empty:
        print("\nNo overlap between OOF predictions and Kalshi history. "
              "Pull more Kalshi history or widen --start/--end.")
        return

    n = len(df)
    traded = df[df["n_targets"] > 0]
    n_trades = traded["n_targets"].sum()
    total_pnl = df["cum_pnl_cents"].iloc[-1]
    total_fees = df["day_fees_cents"].sum()
    total_stake = df["day_stake_cents"].sum()
    win_days = (df["day_pnl_cents"] > 0).sum()
    loss_days = (df["day_pnl_cents"] < 0).sum()
    flat_days = (df["day_pnl_cents"] == 0).sum()

    daily_pnl = df["day_pnl_cents"]
    sharpe = (daily_pnl.mean() / daily_pnl.std() * np.sqrt(252)) if daily_pnl.std() > 0 else 0
    equity = df["bankroll_usd"]
    peak = equity.cummax()
    drawdown = (equity - peak) / peak.replace(0, np.nan)
    max_dd_pct = drawdown.min() * 100 if not drawdown.empty else 0

    print(f"\n{'='*70}")
    print(f"BACKTEST SUMMARY  ({df['date'].min().date()} -> {df['date'].max().date()})")
    print(f"{'='*70}")
    print(f"Settled days        : {n}")
    print(f"Days with trades    : {len(traded)}  ({100*len(traded)/n:.0f}%)")
    print(f"Total positions     : {int(n_trades)}")
    print(f"Total stake         : ${total_stake/100:,.2f}")
    print(f"Total fees          : ${total_fees/100:,.2f}")
    print(f"Realized PnL (cum)  : ${total_pnl/100:+,.2f}")
    print(f"Final bankroll      : ${equity.iloc[-1]:,.2f}  "
          f"(start ${cfg.strategy.bankroll_usd:,.2f})")
    print(f"Daily win/loss/flat : {win_days} / {loss_days} / {flat_days}")
    if n_trades > 0:
        print(f"Edge per position   : {total_pnl/int(n_trades):+.1f}¢")
    print(f"Sharpe (annualized) : {sharpe:.2f}")
    print(f"Max drawdown        : {max_dd_pct:.1f}%")
    print()
    print(f"Model accuracy (on same OOF dates with Kalshi data):")
    print(f"  median |error|    : {df['abs_err_F'].mean():.2f}°F")
    print(f"  in 80% CI         : {100*df['in_80pct_ci'].mean():.1f}%")

    if len(traded) > 0:
        print(f"\nThrottle breakdown:")
        for t, sub in traded.groupby("throttle"):
            pnl = sub["day_pnl_cents"].sum()
            print(f"  throttle={t:.2f}: {len(sub):>3} days, "
                  f"PnL ${pnl/100:+8.2f}, edge {pnl/max(sub['n_targets'].sum(),1):+.1f}¢/trade")

        print(f"\nBy season:")
        for s, sub in traded.groupby("season"):
            pnl = sub["day_pnl_cents"].sum()
            print(f"  {s}: {len(sub):>3} days, PnL ${pnl/100:+8.2f}, "
                  f"edge {pnl/max(sub['n_targets'].sum(),1):+.1f}¢/trade")


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=pd.Timestamp, default=None)
    ap.add_argument("--end",   type=pd.Timestamp, default=None)
    ap.add_argument("--assumed-spread-cents", type=int, default=2,
                    help="half-spread added to trade-price to approximate ask (default 2¢)")
    ap.add_argument("--out", default="data/backtest_results.parquet")
    args = ap.parse_args(argv)

    cfg = load_config()
    oof = load_oof_dataset(cfg.paths.model_dir)
    kalshi = load_kalshi_history(cfg.paths.data_dir / "kalshi_history.parquet")
    cli = pd.read_parquet(cfg.paths.data_dir / f"cli_{cfg.station}.parquet")
    cli["date"] = pd.to_datetime(cli["date"])

    df, positions = backtest(cfg, oof, kalshi, cli, args.start, args.end,
                              args.assumed_spread_cents)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path)
    pos_path = out_path.with_name(out_path.stem + "_positions.parquet")
    positions.to_parquet(pos_path)
    print(f"\nWrote {len(df)} day-rows to {out_path}")
    print(f"Wrote {len(positions)} position-rows to {pos_path}")

    print_summary(df, cfg)
    if not positions.empty:
        print("\nWORST 5 positions:")
        for _, r in positions.nsmallest(5, "net_cents").iterrows():
            print(f"  {r['date'].date()} {r['ticker']:>28s} {r['side']:>3s} x{r['contracts']:>5d}"
                  f" @ {r['entry_cents']:>3d}¢  actual={r['actual_f']:>3d}°F  won={str(r['won']):>5s}"
                  f"  p_model={r['p_model']:.2f}  net={r['net_cents']:+8d}¢")
        print("\nBEST 5 positions:")
        for _, r in positions.nlargest(5, "net_cents").iterrows():
            print(f"  {r['date'].date()} {r['ticker']:>28s} {r['side']:>3s} x{r['contracts']:>5d}"
                  f" @ {r['entry_cents']:>3d}¢  actual={r['actual_f']:>3d}°F  won={str(r['won']):>5s}"
                  f"  p_model={r['p_model']:.2f}  net={r['net_cents']:+8d}¢")

    print("\nCAVEATS:")
    print("  1. Strategy was designed AFTER seeing this data. Edge is an UPPER BOUND.")
    print("  2. Kalshi trades approximate ask via +1¢ half-spread; real fills may slip.")
    print(f"  3. {len(df)} rows is small — confidence intervals are wide. Don't tune knobs on this.")
    print("  4. KXHIGHCHI bucket conventions may have shifted historically — older days might mis-parse.")
    print("  5. For OOS validation: re-run after holding out the last 30+ days.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
