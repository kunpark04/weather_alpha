"""Real-strategy backtest of market_wing across all 20 backfilled cities, flat + Kelly.

Drives the PRODUCTION strategy fn `run_wing_strategy(wing_anchor='market')` over each city's
backfill trade history -- the live config exactly (require_agreement=false, drop_lower_ask,
max_ask_sum=0.90, assumed_win_prob=0.92, fee_aware). market_wing ignores the model, so a dummy
Prediction (peak_P=1.0 -> no regime throttle, matching the model-free live path) is passed.

  * entry: yes_ask at the 1 PM LOCAL anchor (last trade + 1c proxy, via snapshot_at_anchor)
  * truth: Kalshi settlement_value (winning bucket); hold to settlement (no intraday exits)
  * fees: weather_alpha.fees.trade_fee_cents at entry
  * SIGNAL = drop_lower_ask ; PLACEBO = drop_higher_ask (must underperform -- project rule #3)
  * sizing: 'flat' = $2.50/trade (live) ; 'kelly' = quarter-Kelly compounding from $1000

CAVEATS (all optimistic / in-sample): assumes 100% FILL at the ask (live limit-at-ask can
underfill on thin books -- the real binding risk), ask is the +1c proxy, single season
(spring 2026, ~66 days/city), strategy + gate chosen after seeing the data. Feasibility read.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.modal_rank_multicity import CITY, load_city, BACKFILL
from weather_alpha.config import load_config
from weather_alpha.fees import trade_fee_cents
from weather_alpha.kalshi import KalshiContract
from weather_alpha.model import Prediction
from weather_alpha.pmf import INTEGER_F_GRID
from weather_alpha.strategy import run_wing_strategy


def _spec_from_ticker(ticker: str, t_strikes: list[int]) -> str | None:
    """Bucket spec from the ticker suffix (robust: many cities log EMPTY subtitles).
    B49.5 -> '49-50' ; lower T -> '<=n-1' ; upper T -> '>=n+1' (matches parse_kalshi_subtitle)."""
    suf = ticker.split("-")[-1]
    if suf.startswith("B"):
        lo = math.floor(float(suf[1:]))
        return f"{lo}-{lo + 1}"
    if suf.startswith("T") and t_strikes:
        n = int(suf[1:])
        return f"<={n - 1}" if n == min(t_strikes) else f">={n + 1}"
    return None


def _snapshot(day: pd.DataFrame, anchor_utc: pd.Timestamp) -> list[KalshiContract]:
    """KalshiContracts from each ticker's last trade at/before the anchor; specs from tickers,
    yes_ask = last + 1c (the project ask proxy). Independent of the (often-blank) subtitle."""
    pre = day[day["timestamp"] <= anchor_utc]
    if pre.empty:
        return []
    last = pre.sort_values("timestamp").groupby("ticker", as_index=False).last()
    t_strikes = [int(t.split("-")[-1][1:]) for t in last["ticker"] if t.split("-")[-1].startswith("T")]
    out = []
    for _, row in last.iterrows():
        spec = _spec_from_ticker(row["ticker"], t_strikes)
        if spec is None or pd.isna(row["yes_price_cents"]):
            continue
        ym = row["yes_price_cents"] / 100.0
        out.append(KalshiContract(
            ticker=row["ticker"], event_ticker=row.get("event_ticker"), bucket_spec=spec,
            subtitle=row.get("subtitle") or "", strike_type=row.get("strike_type"),
            strike=row.get("floor_strike"),
            yes_bid=max(ym - 0.01, 0.01), yes_ask=min(ym + 0.01, 0.99),
            no_bid=max((1 - ym) - 0.01, 0.01), no_ask=min((1 - ym) + 0.01, 0.99),
            last_price=ym, volume_24h=None, open_interest=None,
            open_time_utc=None, close_time_utc=None, status="active", is_live=True,
        ))
    return out

_SEASON = {12: "DJF", 1: "DJF", 2: "DJF", 3: "MAM", 4: "MAM", 5: "MAM",
           6: "JJA", 7: "JJA", 8: "JJA", 9: "SON", 10: "SON", 11: "SON"}
_UNIFORM = pd.Series(np.full(len(INTEGER_F_GRID), 1.0 / len(INTEGER_F_GRID)),
                     index=INTEGER_F_GRID, name="P")


def _dummy_pred(date: pd.Timestamp) -> Prediction:
    # peak_P=1.0 keeps the regime throttle at 1.0 (no model => no confidence/HRRR throttle).
    return Prediction(date=date, model="market_free", pmf=_UNIFORM, median=70, lo10=60, hi90=80,
                      peak_F=70, peak_P=1.0, season=_SEASON[date.month],
                      hard_floor=None, leaked_below_floor=0.0, nan_features=[])


def run_one(df: pd.DataFrame, series: str, cfg, *, sizing: str,
            drop_lower: bool, drop_higher: bool, bankroll0: float = 1000.0) -> pd.DataFrame:
    tz = ZoneInfo(CITY[series][1])
    flat = 2.5 if sizing == "flat" else None
    bankroll = bankroll0
    cum_cents = 0
    rows = []
    for ev, day in df.groupby("event_date"):
        winners = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(winners) != 1:
            continue
        winner = winners[0]
        anchor_utc = pd.Timestamp(ev.year, ev.month, ev.day, 13, tz=tz).tz_convert("UTC")
        contracts = _snapshot(day, anchor_utc)
        if len(contracts) < 2:
            continue
        out = run_wing_strategy(
            cfg.strategy, _dummy_pred(ev), contracts, None, bankroll,
            require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
            flat_stake_usd=flat, fee_aware=True, sizing_mode="equal_payout",
            drop_lower_ask=drop_lower, drop_higher_ask=drop_higher, max_ask_sum=0.90,
        )
        d_pnl = d_fee = d_stake = n_pos = n_win = 0
        for tgt in out.targets:
            c = next((x for x in contracts if x.ticker == tgt.ticker), None)
            if c is None:
                continue
            price = c.yes_ask if tgt.side == "yes" else c.no_ask
            n = tgt.target_contracts
            cost = int(round(price * 100)) * n
            fee = trade_fee_cents(price, n)
            won = (tgt.ticker == winner) if tgt.side == "yes" else (tgt.ticker != winner)
            net = (n * 100 if won else 0) - cost - fee
            d_pnl += net; d_fee += fee; d_stake += cost; n_pos += 1; n_win += int(won)
        cum_cents += d_pnl
        if sizing == "kelly":
            bankroll = max(0.0, bankroll0 + cum_cents / 100.0)
        rows.append({"date": ev, "pnl_c": d_pnl, "fee_c": d_fee, "stake_c": d_stake,
                     "n_pos": n_pos, "n_win": n_win, "traded": n_pos > 0,
                     "equity": bankroll0 + cum_cents / 100.0})
    return pd.DataFrame(rows)


def metrics(days: pd.DataFrame) -> dict:
    if days.empty:
        return {}
    n = len(days)
    traded = days[days["traded"]]
    pos = int(days["n_pos"].sum()); wins = int(days["n_win"].sum())
    pnl_c = int(days["pnl_c"].sum()); fee_c = int(days["fee_c"].sum())
    dp = days["pnl_c"]
    sharpe = (dp.mean() / dp.std() * np.sqrt(252)) if dp.std() > 0 else 0.0
    eq = days["equity"]; peak = eq.cummax()
    dd_usd = (eq - peak).min()
    dd_pct = ((eq - peak) / peak.replace(0, np.nan)).min() * 100
    return {"days": n, "fire": len(traded) / n, "pos": pos,
            "wr": wins / pos if pos else float("nan"),
            "pnl": pnl_c / 100.0, "fees": fee_c / 100.0,
            "edge_c": pnl_c / pos if pos else float("nan"),
            "sharpe": sharpe, "dd_usd": dd_usd, "dd_pct": dd_pct,
            "final": eq.iloc[-1]}


def main(argv):
    cfg = load_config()
    series_list = [s for s in CITY if (BACKFILL / s).is_dir()]
    flat, kelly, plac = {}, {}, {}
    for s in series_list:
        df = load_city(s)
        if df.empty:
            continue
        flat[s] = metrics(run_one(df, s, cfg, sizing="flat", drop_lower=True, drop_higher=False))
        kelly[s] = metrics(run_one(df, s, cfg, sizing="kelly", drop_lower=True, drop_higher=False))
        plac[s] = metrics(run_one(df, s, cfg, sizing="flat", drop_lower=False, drop_higher=True))

    def table(title, res, kelly_cols=False):
        print(f"\n{'='*84}\n{title}\n{'='*84}")
        if kelly_cols:
            print(f"{'city':<15}{'days':>5}{'fire':>6}{'pos':>5}{'WR':>6}{'PnL$':>9}{'final$':>9}{'Shrp':>6}{'maxDD%':>8}")
            for s, m in sorted(res.items(), key=lambda kv: -(kv[1].get('pnl', -9e9))):
                if not m: continue
                print(f"{CITY[s][0]:<15}{m['days']:>5}{m['fire']:>5.0%}{m['pos']:>5}{m['wr']:>6.0%}"
                      f"{m['pnl']:>+9.2f}{m['final']:>9.0f}{m['sharpe']:>6.2f}{m['dd_pct']:>7.1f}%")
        else:
            print(f"{'city':<15}{'days':>5}{'fire':>6}{'pos':>5}{'WR':>6}{'PnL$':>9}{'fees$':>8}{'edge¢':>7}{'Shrp':>6}{'maxDD$':>8}")
            for s, m in sorted(res.items(), key=lambda kv: -(kv[1].get('pnl', -9e9))):
                if not m: continue
                print(f"{CITY[s][0]:<15}{m['days']:>5}{m['fire']:>5.0%}{m['pos']:>5}{m['wr']:>6.0%}"
                      f"{m['pnl']:>+9.2f}{m['fees']:>8.2f}{m['edge_c']:>+7.1f}{m['sharpe']:>6.2f}{m['dd_usd']:>+8.2f}")
        tot_pnl = sum(m['pnl'] for m in res.values() if m)
        tot_pos = sum(m['pos'] for m in res.values() if m)
        print(f"{'-'*84}\nTOTAL PnL ${tot_pnl:+.2f} across {tot_pos} positions, {len([m for m in res.values() if m])} cities")

    table("FLAT $2.50/trade  --  SIGNAL (drop_lower_ask) = the LIVE config", flat)
    table("QUARTER-KELLY (start $1000, compounding)  --  SIGNAL (drop_lower_ask)", kelly, kelly_cols=True)
    table("PLACEBO (drop_HIGHER_ask), flat $2.50  --  must UNDERPERFORM the signal", plac)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
