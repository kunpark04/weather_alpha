#!/usr/bin/env bash
# Quick view of the latest directional PAPER captures/entries + bankroll. Run on the droplet:
#   ssh weather-alpha@137.184.128.37 'bash weather-alpha/scripts/paper_peek.sh'
cd "$HOME/weather-alpha"
PY="$HOME/weather-alpha/.venv/bin/python"; [ -x "$PY" ] || PY=python3
"$PY" - <<'PY'
import os, json, pandas as pd
base = "data/directional_paper"
for m in ("high", "low"):
    f = f"{base}/{m}_log.parquet"
    if not os.path.exists(f):
        print(f"[{m}] no captures yet"); continue
    d = pd.read_parquet(f)
    day = d["event_date"].max()
    dd = d[d["event_date"] == day]
    p = dd[dd["picked"] == True]  # noqa: E712
    print(f"[{m}] {day}: {len(dd)} scanned, {len(p)} ENTERED, {int(dd['settled'].sum())} settled")
    if len(p):
        cols = [c for c in ["city", "ticker", "confidence_mid", "fillable_pct",
                            "ladder_depth_usd", "win", "paper_pnl_usd"] if c in p.columns]
        print(p[cols].to_string(index=False))
s = f"{base}/paper_state.json"
if os.path.exists(s):
    st = json.load(open(s))
    for m in ("high", "low"):
        b = st.get(m, {}).get("bankroll", 0.0)
        print(f"  {m} bankroll ${b:.2f}  entries_today: {st.get(m, {}).get('entries', {})}")
PY
