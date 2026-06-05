"""Is the paper bot's Jun-4 Houston wing profitable? Determine the actual KHOU high.

Wing (equal-payout, K=3): B82.5 (82-83) + B84.5 (84-85), 3 contracts each -> pays $3 if the
settled high lands in 82-85, else $0. Cost = 3*28c + 3*51c = 237c + 11c fee = 248c. So:
  covered (high in 82-85): +300 - 248 = +52c  (+$0.52)
  missed  (high outside)  :   0  - 248 = -248c  (-$2.48)
The Kalshi market isn't settled yet (CLI high posts next morning), so we read the actual high from
KHOU (Houston Hobby = the settlement station) ASOS obs via Iowa Mesonet. NOTE: the official CLI high
can differ from raw ASOS by ~1F; if the high is right on the 81/82 or 85/86 boundary, treat as TBD.
"""
from __future__ import annotations

from io import StringIO

import pandas as pd
import requests

# bucket edges for KXHIGHTHOU: <=81 (T82) | 82-83 (B82.5) | 84-85 (B84.5) | 86-87 | 88-89 | >=90 (T89)
WING_LO, WING_HI = 82, 85   # the wing covers a high in [82, 85]
COST_C, FEE_C, PAYOUT_C = 237, 11, 300


def khou_high(date="2026-06-04"):
    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
    nxt = (pd.Timestamp(date) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    p = {"station": "HOU", "data": "tmpf",
         "year1": date[:4], "month1": int(date[5:7]), "day1": int(date[8:10]),
         "year2": nxt[:4], "month2": int(nxt[5:7]), "day2": int(nxt[8:10]),
         "tz": "America/Chicago", "format": "onlycomma", "missing": "M"}
    df = pd.read_csv(StringIO(requests.get(url, params=p, timeout=60).text))
    df["t"] = pd.to_numeric(df["tmpf"], errors="coerce")
    df["ts"] = pd.to_datetime(df["valid"], errors="coerce")
    day = df[df["ts"].dt.strftime("%Y-%m-%d") == date].dropna(subset=["t"])
    return (float(day["t"].max()), len(day)) if len(day) else (None, 0)


def main():
    high, n = khou_high("2026-06-04")
    if high is None:
        print("no KHOU obs for 2026-06-04 yet"); return 1
    bucket = ("<=81" if high < 82 else "82-83" if high < 84 else "84-85" if high < 86
              else "86-87" if high < 88 else "88-89" if high < 90 else ">=90")
    covered = WING_LO <= high <= WING_HI
    net = (PAYOUT_C if covered else 0) - COST_C - FEE_C
    print(f"KHOU (Houston Hobby) high on 2026-06-04: {high:.0f} F   ({n} obs; bucket {bucket})")
    print(f"wing covers 82-85 -> {'COVERED' if covered else 'MISSED'}")
    print(f"PnL = {'+300' if covered else '0'}c payout - 237c cost - 11c fee = "
          f"{net:+d}c  ({net/100:+.2f} USD)")
    if not covered and high >= 86:
        print("Houston's early-June high ran hot (>=86) -> above the wing; the wing was centered too low.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
