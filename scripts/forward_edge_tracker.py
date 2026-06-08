"""Forward-edge tracker -- accumulate post-go-live REALIZED fires vs the §1.7 +4.6c benchmark.

The missing `FWD-TRACK` rollup (HANDOFF §7). `check_live_execution.py` is a mock safety-fix verifier;
the walkforward scripts read the BACKFILL tape, not the live log. This reads the bot's OWN
`live_log.parquet` (the real entries/fills/fees it placed) and settles each fire against the backfill
tape's Kalshi `settlement_value` (reused via `load_city` -- no new network/auth), then reports the
running forward edge with N, win rate, $ PnL, the favorite-longshot binomial-vs-breakeven test, and a
comparison to the **+4.6c/$payout realistic-OOS benchmark** from
`scripts/chicago_oos_netfee.py` (HANDOFF §1.7), including how many more settled fires are needed for
significance.

Realized PnL is reconstructed from REAL fills (not the optimistic ask proxy): for each filled leg,
net = contracts*100 (if its ticker is the settled winner) - contracts*fill_price - fee. Truth is the
backfill `settlement_value=='yes'` ticker for that anchor date; a fire with no truth yet is OPEN
(counted, not scored) -- so the tracker advances as settled days land in the tape.

Two independent views are printed: (1) this reconstruction, and (2) the Book's own running
`realized_pnl_cents` from `positions.json`, as a cross-check.

NOTE (2026-06-08): local `data/live_log.parquet` + `positions.json` are STALE since 2026-05-30 (the
droplet->local pull is the blocker, HANDOFF §1.7 / §7 FWD-TRACK). Until that is unblocked there are no
post-go-live fires to score; `--since 2026-05-01` replays the existing May paper fills to validate the
pipeline end-to-end.

Usage:
  python scripts/forward_edge_tracker.py                      # paper, KXHIGHCHI, since 2026-06-02
  python scripts/forward_edge_tracker.py --mode live
  python scripts/forward_edge_tracker.py --since 2026-05-01   # smoke-test on existing May paper fills
  python scripts/forward_edge_tracker.py --series KXHIGHCHI --benchmark 4.6
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:                                  # Windows consoles default to cp1252 -> can't encode §/⚠️
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from scripts.modal_rank_multicity import CITY, load_city
from weather_alpha.fees import trade_fee_cents

GO_LIVE = "2026-06-02"          # weather-alpha-live.service enabled (HANDOFF §7 L2/DEPLOY)
BENCHMARK_C = 4.6               # realistic-OOS edge, c/$payout, real ceil fee (HANDOFF §1.7)


def _winners(series: str) -> dict:
    """anchor-date (normalized Timestamp) -> winning ticker, from the backfill settlement truth."""
    df = load_city(series)
    if df.empty:
        return {}
    out = {}
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) == 1:
            out[pd.Timestamp(ev).normalize()] = w[0]
    return out


def _fires(log_path: Path, mode: str, since: pd.Timestamp, series: str) -> pd.DataFrame:
    """One row per (anchor_date, ticker) FILLED leg at/after `since` for `mode` AND `series`.

    The resident PAPER log holds ALL cities, so filtering by the series ticker prefix is essential
    (without it, other cities' legs get matched against this series' winner -> bogus 0% coverage).
    """
    if not log_path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(log_path)
    df = df[(df.get("mode") == mode) & (df.get("fill_status") == "filled")].copy()
    if df.empty:
        return df
    df = df[df["ticker"].astype(str).str.startswith(f"{series}-")]   # this city only (e.g. KXHIGHCHI- excludes KXHIGHTHOU-)
    if df.empty:
        return df
    df["anchor_date"] = pd.to_datetime(df["anchor_date"]).dt.normalize()
    df = df[df["anchor_date"] >= since]
    df["fill_contracts"] = pd.to_numeric(df["fill_contracts"], errors="coerce").fillna(0).astype(int)
    df = df[df["fill_contracts"] > 0]
    if df.empty:
        return df
    # collapse retries: keep the latest filled row per (anchor_date, ticker)
    df = df.sort_values("run_utc").groupby(["anchor_date", "ticker"], as_index=False).last()
    return df


def _per_fire(legs: pd.DataFrame, winner: str | None) -> dict:
    """Realized net for one fire (one anchor_date's filled legs) given the settled winner."""
    net_c = cost_c = fee_c = 0
    sizes = []
    for _, r in legs.iterrows():
        n = int(r["fill_contracts"])
        price = float(r["fill_price_cents"]) if pd.notna(r["fill_price_cents"]) else r["yes_ask"] * 100
        fee = int(r["fee_cents"]) if pd.notna(r.get("fee_cents")) else trade_fee_cents(price / 100.0, n)
        won = (winner is not None) and (r["ticker"] == winner)
        cost_c += int(round(price)) * n
        fee_c += fee
        net_c += (n * 100 if won else 0) - int(round(price)) * n - fee
        sizes.append(n)
    cov = int(any(legs["ticker"] == winner)) if winner is not None else None
    base = float(np.mean(sizes)) if sizes else float("nan")     # payout base ~ contracts/leg (= benchmark unit)
    return {"net_c": net_c, "cost_c": cost_c, "fee_c": fee_c, "cov": cov, "base": base}


def _binom_sf(wins: int, n: int, p0: float) -> float:
    p0 = min(max(p0, 1e-9), 1 - 1e-9)
    return math.fsum(math.comb(n, k) * p0 ** k * (1 - p0) ** (n - k) for k in range(wins, n + 1))


def main(argv) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    ap.add_argument("--series", default="KXHIGHCHI")
    ap.add_argument("--since", default=GO_LIVE, help=f"only fires on/after this date (default go-live {GO_LIVE})")
    ap.add_argument("--benchmark", type=float, default=BENCHMARK_C, help="c/$payout edge to compare against (§1.7)")
    ap.add_argument("--log", default=None,
                    help="live_log.parquet to read (default data/<mode>/live_log.parquet — the resident bot's path)")
    args = ap.parse_args(argv)

    since = pd.Timestamp(args.since).normalize()
    city = CITY.get(args.series, (args.series, "?"))[0]

    # The resident bots write isolated per-mode state (config/{paper,live}.yaml): data/<mode>/live_log.parquet.
    log_path = Path(args.log) if args.log else (_ROOT / "data" / args.mode / "live_log.parquet")
    if not log_path.is_absolute():
        log_path = _ROOT / log_path
    pos_path = log_path.parent / "positions.json"
    fires = _fires(log_path, args.mode, since, args.series)

    print("=" * 86)
    print(f"FORWARD-EDGE TRACKER  --  {city} ({args.series})  mode={args.mode}  since {since.date()}")
    print(f"log: {log_path}  benchmark: +{args.benchmark:.1f}c/$payout (realistic-OOS, §1.7)")
    # staleness: latest anchor in the WHOLE log vs the cutoff
    if log_path.exists():
        _all = pd.read_parquet(log_path)
        _all = _all[(_all.get("mode") == args.mode) & _all["ticker"].astype(str).str.startswith(f"{args.series}-")]
        last = pd.to_datetime(_all["anchor_date"]).max() if len(_all) else None
        stale = f"latest {args.mode} anchor in log: {last.date() if last is not None else 'n/a'}"
        if last is not None and last < since:
            stale += f"  ⚠️ no fires on/after {since.date()} (run scripts/pull-state.ps1 to refresh; §7 FWD-TRACK)"
        print(stale)
    print("=" * 86)

    if fires.empty:
        print(f"\nNo FILLED {args.mode} fires on/after {since.date()} in {log_path}.")
        print("Run scripts/pull-state.ps1 to refresh from the droplet, or lower --since to smoke-test.")
        _book_crosscheck(pos_path)
        return 0

    winners = _winners(args.series)
    recs = []
    for ad, legs in fires.groupby("anchor_date"):
        w = winners.get(ad)                          # None => not settled in the tape yet
        pf = _per_fire(legs, w)
        pf.update({"date": ad, "settled": w is not None, "n_legs": len(legs)})
        recs.append(pf)
    d = pd.DataFrame(recs).sort_values("date")
    settled = d[d["settled"]].copy()
    open_n = int((~d["settled"]).sum())

    print(f"\nfires: {len(d)} total  |  settled (scored): {len(settled)}  |  open (awaiting truth): {open_n}")
    if open_n:
        od = d[~d["settled"]]["date"]
        print(f"  open dates: {', '.join(str(x.date()) for x in od)}  (winner not yet in backfill tape)")

    if settled.empty:
        print("\nNo settled forward fires to score yet -- truth lands as the backfill tape catches up.")
        _book_crosscheck(pos_path)
        return 0

    n = len(settled)
    wins = int(settled["cov"].sum())
    net_c = settled["net_c"]
    norm = (settled["net_c"] / settled["base"])      # c/$payout, comparable to the benchmark
    mean = norm.mean()
    se = norm.std(ddof=1) / np.sqrt(n) if n > 1 else float("nan")
    be = ((settled["cost_c"] + settled["fee_c"]) / (settled["base"] * 100.0)).mean()
    binp = _binom_sf(wins, n, be)

    print(f"\n{'metric':<26}{'value':>16}")
    print("-" * 42)
    print(f"{'settled fires (N)':<26}{n:>16}")
    print(f"{'win rate':<26}{settled['cov'].mean():>15.0%}")
    print(f"{'realized PnL ($)':<26}{net_c.sum()/100.0:>+16.2f}")
    print(f"{'fees paid ($)':<26}{settled['fee_c'].sum()/100.0:>16.2f}")
    print(f"{'mean c/trade':<26}{net_c.mean():>+16.1f}")
    print(f"{'edge c/$payout':<26}{mean:>+16.1f}")
    print(f"{'per-day SE':<26}{se:>16.1f}")
    print(f"{'breakeven WR':<26}{be:>15.0%}")
    print(f"{'binomial p (WR>BE)':<26}{binp:>16.3f}")

    print(f"\nvs benchmark +{args.benchmark:.1f}c/$payout (§1.7 realistic-OOS):")
    print(f"  observed {mean:+.1f}  -- {'ABOVE' if mean >= args.benchmark else 'BELOW'} benchmark "
          f"by {mean-args.benchmark:+.1f}c")
    if se == se and se > 0:
        z = (mean - args.benchmark) / se
        print(f"  z vs benchmark = {z:+.1f} (|z|<2 => consistent with the benchmark at this N)")
        sig = "YES" if abs(mean) / se >= 1.96 else "no"
        print(f"  edge distinguishable from 0?  {sig}  (t={mean/se:+.1f})")
        if mean > 0 and abs(mean) / se < 1.96:
            need = math.ceil((1.96 * norm.std(ddof=1) / mean) ** 2)
            print(f"  ~fires to significance at the current mean/var: {need} (have {n}, need ~{max(0,need-n)} more)")
    print("\n  NOTE: forward fires are real fills/fees but reconstruct truth from the backfill tape;")
    print("  partial/unequal fills use mean(contracts) as the $payout base. Sample is tiny early --")
    print("  treat <20 settled fires as anecdote (project rule #4).")

    _book_crosscheck(pos_path)
    return 0


def _book_crosscheck(pj: Path) -> None:
    """Independent view: the Book's own running realized PnL from positions.json."""
    if not pj.exists():
        return
    b = json.loads(pj.read_text(encoding="utf-8"))
    pos = b.get("positions", {})
    n_settled = sum(1 for p in pos.values() if p.get("settled"))
    print(f"\nBook cross-check (positions.json): realized ${b.get('realized_pnl_cents',0)/100:+.2f}, "
          f"fees ${b.get('fees_paid_cents',0)/100:.2f}, {len(pos)} positions in book "
          f"({n_settled} settled, {len(pos)-n_settled} open).")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
