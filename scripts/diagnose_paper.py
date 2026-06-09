"""One-off diagnostic: WHY is the paper bot down ~-$9.42 and account-halted?

Reads data/paper/positions.json (the bot's own Book = ground truth for entries/fills/realized)
and joins each cohort to the settlement tape (data/backfill) to find the winning bucket, so we
can classify every city-day wing as COVER (a leg won) or MISS (high fell outside the 2-leg wing)
and see where the misses landed. Also re-derives the 50%% account-drawdown latch point to confirm
the halt is a correct circuit-breaker, not a degenerate-state artifact (account_hwm=0/bankroll=null).
"""
from __future__ import annotations

import glob
import io
import json
import zipfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BACKFILL = ROOT / "data" / "backfill"
PAPER = ROOT / "data" / "paper" / "positions.json"

BANKROLL0 = 2500          # config/paper.yaml strategy.bankroll_usd = 25.0
ACCT_PCT = 0.50           # account_drawdown_pct

MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
       "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def parse_ticker(t: str):
    # KXHIGHTHOU-26JUN04-B82.5 -> ("KXHIGHTHOU", date(2026-06-04), "B82.5")
    series, datecode, strike = t.split("-", 2)
    yy, mon, dd = int(datecode[:2]), datecode[2:5], int(datecode[5:7])
    return series, f"20{yy:02d}-{MON[mon]:02d}-{dd:02d}", strike


def winner_and_grid(series: str, date_iso: str):
    """From the backfill zip for this event: winning bucket's floor_strike + ordered floor list."""
    zp = BACKFILL / series / f"{date_iso}.zip"
    if not zp.exists():
        return None, None
    z = zipfile.ZipFile(str(zp))
    df = pd.read_parquet(io.BytesIO(z.read(z.namelist()[0])))
    win = df.loc[df["settlement_value"] == "yes", "floor_strike"].dropna().unique()
    floors = sorted(df["floor_strike"].dropna().unique())
    return (float(win[0]) if len(win) == 1 else None), floors


def main():
    book = json.loads(PAPER.read_text())
    pos = book["positions"]

    # group legs by (series, date)
    cohorts: dict[tuple, list] = {}
    for key, p in pos.items():
        series, date_iso, strike = parse_ticker(p["ticker"])
        cohorts.setdefault((series, date_iso), []).append(p)

    rows = []
    for (series, date_iso), legs in sorted(cohorts.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        settled = [l for l in legs if l["settled"]]
        win_floor, floors = winner_and_grid(series, date_iso)
        leg_floors = []
        for l in legs:
            # floor of each entered leg = its bucket's floor_strike (match by ticker in tape)
            leg_floors.append(l)
        realized = sum(l["realized_pnl_cents"] for l in settled)
        stake = sum(int(l["contracts"] * l["avg_cost_cents"]) for l in legs)
        n_win = sum(1 for l in settled if l["realized_pnl_cents"] > 0)
        cover = n_win > 0
        # locate winner vs entered legs (need entered floors from tape)
        ent_floors = entered_floors(series, date_iso, {l["ticker"] for l in legs})
        dist = None
        if win_floor is not None and ent_floors:
            dist = min(abs(win_floor - f) for f in ent_floors)  # bucket-width units (2°F)
        rows.append({
            "date": date_iso, "city": series.replace("KXHIGH", ""), "legs": len(legs),
            "settled": len(settled), "stake_$": stake / 100, "realized_$": realized / 100,
            "cover": cover, "win_floor": win_floor,
            "ent_floors": ",".join(f"{f:g}" for f in sorted(ent_floors)) if ent_floors else "?",
            "miss_dist_F": dist,
        })

    r = pd.DataFrame(rows)
    settled_r = r[r["settled"] > 0].copy()

    pd.set_option("display.width", 200, "display.max_columns", 30)
    print("=" * 100)
    print("PER COHORT (city-day wing): entered legs, winner, cover?, realized")
    print("=" * 100)
    print(settled_r.to_string(index=False))

    # --- cover vs miss economics ------------------------------------------------
    cov = settled_r[settled_r["cover"]]
    miss = settled_r[~settled_r["cover"]]
    print("\n" + "=" * 60)
    print("COVER vs MISS economics  (settled cohorts only)")
    print("=" * 60)
    print(f"cohorts settled      : {len(settled_r)}")
    print(f"  COVER (a leg won)  : {len(cov):2d}  ({len(cov)/len(settled_r):.0%})   "
          f"avg realized {cov['realized_$'].mean():+.2f}   total {cov['realized_$'].sum():+.2f}")
    print(f"  MISS  (high outside): {len(miss):2d}  ({len(miss)/len(settled_r):.0%})   "
          f"avg realized {miss['realized_$'].mean():+.2f}   total {miss['realized_$'].sum():+.2f}")
    ev = settled_r["realized_$"].mean()
    print(f"EV / cohort          : {ev:+.2f}   (x{len(settled_r)} settled = {settled_r['realized_$'].sum():+.2f})")
    print("\nMISS distance of the true high from the nearest entered leg (°F; bucket width = 2):")
    print(miss[["date", "city", "win_floor", "ent_floors", "miss_dist_F", "realized_$"]].to_string(index=False))

    # --- halt latch reconstruction ---------------------------------------------
    print("\n" + "=" * 60)
    print("HALT reconstruction (50% account drawdown, paper balance = $25 + realized)")
    print("=" * 60)
    # settlement-time order
    order = []
    for key, p in pos.items():
        if p["settled"]:
            order.append((p["settled_utc"], p["realized_pnl_cents"], p["ticker"]))
    order.sort()
    cum = 0
    hwm = 0
    latched_at = None
    for ts, rl, tk in order:
        cum += rl
        hwm = max(hwm, cum)
        bal = BANKROLL0 + cum
        dd = hwm - cum
        trip = dd >= ACCT_PCT * bal and bal > 0
        if trip and latched_at is None:
            latched_at = (ts, tk, cum, bal, dd)
    print(f"final cumulative realized : ${cum/100:+.2f}   (book says ${book['realized_pnl_cents']/100:+.2f})")
    print(f"account HWM (peak realized): ${hwm/100:+.2f}   (book says ${book['account_hwm_cents']/100:+.2f})")
    if latched_at:
        ts, tk, c, bal, dd = latched_at
        print(f"50% latch FIRST satisfied : {ts}  after {tk}")
        print(f"   at cum=${c/100:+.2f}, balance=${bal/100:.2f}, drawdown=${dd/100:.2f} >= 50% of ${bal/100:.2f} (${ACCT_PCT*bal/100:.2f})")
    else:
        print("50% latch: never satisfied in reconstruction (!)")
    print(f"book account_halted       : {book['account_halted']}")
    return 0


def entered_floors(series, date_iso, tickers):
    zp = BACKFILL / series / f"{date_iso}.zip"
    if not zp.exists():
        return []
    z = zipfile.ZipFile(str(zp))
    df = pd.read_parquet(io.BytesIO(z.read(z.namelist()[0])))
    sub = df[df["ticker"].isin(tickers)][["ticker", "floor_strike"]].dropna().drop_duplicates()
    return sorted(sub["floor_strike"].unique())


if __name__ == "__main__":
    raise SystemExit(main())
