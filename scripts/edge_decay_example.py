"""Explicit REAL example of why late-anchor 'edge' decays into leakage: one actual Chicago day.

Picks a real KXHIGHCHI event-day (middle-bucket winner, trades spanning morning->4pm) and prints
the order book (each bucket's last-traded yes price) at 8a/10a/11a/12p/1p/2p/3p/4p, plus the
production 2-leg wing's cost and whether it covers the eventual winner. The WINNER's price climbing
toward $1.00 across the afternoon IS the daily high being realized; so a wing that 'covers' at 3-4pm
is pricing an already-known outcome -- the apparent edge there is leakage, not a forecast you could
trade. The tradeable window is midday, where the wing is still cheap vs an uncertain outcome.
"""
from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from scripts.backtest_multicity import _dummy_pred, _snapshot
from scripts.modal_rank_multicity import load_city
from weather_alpha.config import load_config
from weather_alpha.strategy import run_wing_strategy

CT = ZoneInfo("America/Chicago")
ANCHORS = [8, 10, 11, 12, 13, 14, 15, 16]


def book_at(day, anchor_utc):
    pre = day[day["timestamp"] <= anchor_utc].dropna(subset=["yes_price_cents"])
    if pre.empty:
        return {}
    last = pre.sort_values("timestamp").groupby("ticker")["yes_price_cents"].last()
    return {t: v / 100.0 for t, v in last.items()}


def tlabel(t):
    return t.split("-")[-1]


def pick_day(df):
    best, best_rise = None, -9.0
    for ev, day in df.groupby("event_date"):
        w = day.loc[day["settlement_value"] == "yes", "ticker"].unique()
        if len(w) != 1 or not tlabel(w[0]).startswith("B"):     # middle bucket = more illustrative
            continue
        winner = w[0]
        b9 = book_at(day, pd.Timestamp(ev.year, ev.month, ev.day, 9, tz=CT).tz_convert("UTC"))
        b16 = book_at(day, pd.Timestamp(ev.year, ev.month, ev.day, 16, tz=CT).tz_convert("UTC"))
        if winner not in b9 or winner not in b16 or len(b9) < 5 or len(b16) < 5:
            continue
        rise = b16[winner] - b9[winner]
        if rise > best_rise:
            best, best_rise = (ev, day, winner), rise
    return best


def main():
    cfg = load_config()
    df = load_city("KXHIGHCHI")
    picked = pick_day(df)
    if not picked:
        print("no clean example day found"); return 1
    ev, day, winner = picked
    print("=" * 92)
    print(f"REAL EXAMPLE -- Chicago (KXHIGHCHI) {ev.date()}   winner = {tlabel(winner)} (settled YES)")
    print("=" * 92)
    print(f"{'anchor':>7}{'winnerPx':>10}{'wing(2-leg)':>26}{'cost':>7}{'gate<.90':>10}{'covers?':>9}")
    traj = []
    for h in ANCHORS:
        anchor = pd.Timestamp(ev.year, ev.month, ev.day, h, tz=CT).tz_convert("UTC")
        book = book_at(day, anchor)
        wpx = book.get(winner, float("nan"))
        traj.append((h, wpx))
        contracts = _snapshot(day, anchor)
        wing_s, cost, covers, gate = "(no book)", float("nan"), "-", "-"
        if len(contracts) >= 2:
            out = run_wing_strategy(
                cfg.strategy, _dummy_pred(ev), contracts, None, 1000.0,
                require_agreement=False, wing_anchor="market", assumed_win_prob=0.92,
                flat_stake_usd=2.5, fee_aware=True, sizing_mode="equal_payout",
                drop_lower_ask=True, drop_higher_ask=False, max_ask_sum=0.90)
            sa = out.diagnostics.get("sum_asks")
            if out.targets:
                tk = [t.ticker for t in out.targets]
                wing_s = "+".join(tlabel(t) for t in tk)
                cost = sa
                covers = "YES" if winner in tk else "no"
                gate = "PASS"
            elif sa is not None:
                wing_s = "+".join(tlabel(c.ticker) for c in sorted(
                    contracts, key=lambda c: -c.yes_ask)[:2])
                cost, gate = sa, ("PASS" if sa < 0.90 else "FAIL>=.90")
        h12 = f"{(h - 12) or 12 if h >= 12 else h}{'p' if h >= 12 else 'a'}"
        cs = f"{cost:.3f}" if cost == cost else "  -"
        wp = f"{wpx:.2f}" if wpx == wpx else "  -"
        print(f"{h12:>7}{wp:>10}{wing_s:>26}{cs:>7}{gate:>10}{covers:>9}")

    # full 6-bucket book at four key anchors
    print("\nfull book (bucket=yes_price) at key anchors:")
    for h in (9, 13, 15, 16):
        anchor = pd.Timestamp(ev.year, ev.month, ev.day, h, tz=CT).tz_convert("UTC")
        book = book_at(day, anchor)
        h12 = f"{(h - 12) or 12 if h >= 12 else h}{'p' if h >= 12 else 'a'}"
        items = " ".join(f"{tlabel(t)}={p:.2f}" + ("*" if t == winner else "")
                         for t, p in sorted(book.items(), key=lambda kv: -kv[1]))
        print(f"  {h12:>4}: {items}")

    w0 = next((p for h, p in traj if h == 9 and p == p), traj[0][1])
    print(f"\nDECAY: winner price 9a~{traj[0][1]:.2f} -> 1p~{dict(traj).get(13, float('nan')):.2f} "
          f"-> 4p~{dict(traj).get(16, float('nan')):.2f}. By mid-afternoon the high is (nearly) IN, so")
    print("the winning bucket trades near $1 -- a wing that 'covers' then is pricing a known outcome.")
    print("That is the realized-high leakage the wide grid shows at 3-4p: high coverage, but NOT a")
    print("tradeable forecast (live, the ask would already be ~$1; the +1c proxy understates it).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
