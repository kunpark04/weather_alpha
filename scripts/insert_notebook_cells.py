"""Insert a new section 10.3 'Kalshi-bucket accuracy: model vs market, agreement-split'
into notebooks/model_v3.ipynb, immediately after the existing section 10.2 cells.
Idempotent: if the new section is already present, do nothing.
"""

from __future__ import annotations
import json
from pathlib import Path

NB_PATH = Path("notebooks/model_v3.ipynb")
nb = json.loads(NB_PATH.read_text(encoding="utf-8"))

SECTION_MARKER = "### 10.3 Kalshi-bucket accuracy"

# Idempotency check
for c in nb["cells"]:
    src = "".join(c.get("source", []))
    if SECTION_MARKER in src:
        print("Section 10.3 already present - nothing to do.")
        raise SystemExit(0)

md_source = [
    "### 10.3 - Kalshi-bucket accuracy: model vs market, split by agreement\n",
    "\n",
    "**Why this cell exists.** Section 10.2 computes top-1 / top-3 against synthetic 2F-pair buckets that span the whole INTEGER_F_GRID. The actual Kalshi KXHIGHCHI buckets are different: four 2F-wide middle buckets centered around the day's expected high, plus two open-ended tails (e.g. `71 or below`, `80 or above`). Settlement accuracy at *that* bucketing is what matters for trading.\n",
    "\n",
    "**What this cell measures.** On every day where we have Kalshi history (2026-03-21 onward), at the 1 PM local anchor:\n",
    "- `model_modal` = Kalshi contract with the highest model-integrated probability over its bucket range\n",
    "- `market_modal` = Kalshi contract with the highest `yes_ask`\n",
    "- `truth_bucket` = the Kalshi contract whose range contains the realized `cli_high`\n",
    "\n",
    "Reports top-1 (modal hits truth bucket) and top-3 (truth is in modal or one of its two positional adjacents) for the model and the market, both overall and split by whether the two modals agreed.\n",
    "\n",
    "**Why agreement matters.** The `wing + drop_lower_ask` production strategy fires only on agreement days. The top-3 on agreement days tells us the structural ceiling on that strategy's hit rate."
]

code_source = [
    "# Kalshi-bucket-resolution accuracy diagnostic.\n",
    "#\n",
    "# Uses the ACTUAL Kalshi bucket specs per day (2F middles + 2 open tails),\n",
    "# not the synthetic 2F pairs from 10.2. Only the 2026-03-21+ window where\n",
    "# we have Kalshi history.\n",
    "import re\n",
    "\n",
    "\n",
    "def _parse_kalshi_subtitle(subtitle):\n",
    "    \"\"\"Parse e.g. '72 to 73', '71 or below', '80 or above' -> bucket spec str.\n",
    "    Handles both ASCII '-' and unicode dashes; degree sign is stripped.\"\"\"\n",
    "    if not isinstance(subtitle, str):\n",
    "        return None\n",
    "    s = re.sub(r\"[^\\x00-\\x7f]\", \"\", subtitle).strip().lower()\n",
    "    m = re.match(r\"(\\d+)\\s*(?:to|-)\\s*(\\d+)\", s)\n",
    "    if m:\n",
    "        return f\"{m.group(1)}-{m.group(2)}\"\n",
    "    m = re.match(r\"(\\d+)\\s*or\\s*below\", s)\n",
    "    if m:\n",
    "        return f\"<={m.group(1)}\"\n",
    "    m = re.match(r\"(\\d+)\\s*or\\s*above\", s)\n",
    "    if m:\n",
    "        return f\">={m.group(1)}\"\n",
    "    return None\n",
    "\n",
    "\n",
    "def _bucket_lower_bound(spec):\n",
    "    if spec.startswith(\"<=\"):\n",
    "        return int(spec[2:]) - 100\n",
    "    if spec.startswith(\">=\"):\n",
    "        return int(spec[2:])\n",
    "    return int(spec.split(\"-\")[0])\n",
    "\n",
    "\n",
    "def _bucket_prob_from_pmf(pmf, spec, F_grid=INTEGER_F_GRID):\n",
    "    \"\"\"Integrate calibrated PMF over the integer-F range covered by `spec`.\"\"\"\n",
    "    pred, _ = _parse_bucket(spec) if not isinstance(spec, str) else (None, None)\n",
    "    if spec.startswith(\"<=\"):\n",
    "        thr = int(spec[2:])\n",
    "        mask = F_grid <= thr\n",
    "    elif spec.startswith(\">=\"):\n",
    "        thr = int(spec[2:])\n",
    "        mask = F_grid >= thr\n",
    "    else:\n",
    "        lo, hi = (int(x) for x in spec.split(\"-\"))\n",
    "        mask = (F_grid >= lo) & (F_grid <= hi)\n",
    "    return float(pmf[mask].sum())\n",
    "\n",
    "\n",
    "def _spec_contains(spec, value):\n",
    "    if spec.startswith(\"<=\"):\n",
    "        return value <= int(spec[2:])\n",
    "    if spec.startswith(\">=\"):\n",
    "        return value >= int(spec[2:])\n",
    "    lo, hi = (int(x) for x in spec.split(\"-\"))\n",
    "    return lo <= value <= hi\n",
    "\n",
    "\n",
    "# Load Kalshi history (only present locally; will fail gracefully if missing).\n",
    "_kalshi_path = DATA_DIR / \"kalshi_history.parquet\"\n",
    "if not _kalshi_path.exists():\n",
    "    print(f\"WARNING: {_kalshi_path} not found. Run scripts/kalshi_history.py first.\")\n",
    "else:\n",
    "    _k = pd.read_parquet(_kalshi_path)\n",
    "    _k[\"event_date\"] = pd.to_datetime(_k[\"event_date\"]).dt.normalize()\n",
    "    _k[\"timestamp\"] = pd.to_datetime(_k[\"timestamp\"], utc=True)\n",
    "\n",
    "    _valid_idx = ~np.isnan(bucket_probs_calib).any(axis=1) & ~np.isnan(actuals)\n",
    "    _probs = bucket_probs_calib[_valid_idx]\n",
    "    _act = actuals[_valid_idx].astype(int)\n",
    "    _dates = pd.to_datetime(feature_df[\"date\"].values[_valid_idx]).normalize()\n",
    "    _date_to_i = {d: i for i, d in enumerate(_dates)}\n",
    "\n",
    "    _records = []\n",
    "    for _ev in sorted(_k[\"event_date\"].unique()):\n",
    "        _ev_ts = pd.Timestamp(_ev).normalize()\n",
    "        if _ev_ts not in _date_to_i:\n",
    "            continue\n",
    "        _actual_f = int(_act[_date_to_i[_ev_ts]])\n",
    "        _anchor = (_ev_ts.tz_localize(LOCAL_TZ) + pd.Timedelta(hours=13)).tz_convert(\"UTC\")\n",
    "        _pre = _k[(_k[\"event_date\"] == _ev_ts) & (_k[\"timestamp\"] <= _anchor)]\n",
    "        if _pre.empty:\n",
    "            continue\n",
    "        _latest = _pre.sort_values(\"timestamp\").groupby(\"ticker\", as_index=False).last()\n",
    "        _pmf = _probs[_date_to_i[_ev_ts]]\n",
    "\n",
    "        _bks = []\n",
    "        for _, _row in _latest.iterrows():\n",
    "            _spec = _parse_kalshi_subtitle(_row.get(\"subtitle\"))\n",
    "            if _spec is None:\n",
    "                continue\n",
    "            _yes_ask = min((_row[\"yes_price_cents\"] or 50) / 100 + 0.01, 0.99)\n",
    "            _p = _bucket_prob_from_pmf(_pmf, _spec)\n",
    "            _bks.append({\"spec\": _spec, \"yes_ask\": _yes_ask, \"p_model\": _p})\n",
    "        if len(_bks) < 2:\n",
    "            continue\n",
    "        _bks.sort(key=lambda c: _bucket_lower_bound(c[\"spec\"]))\n",
    "\n",
    "        _model_i  = max(range(len(_bks)), key=lambda j: _bks[j][\"p_model\"])\n",
    "        _market_i = max(range(len(_bks)), key=lambda j: _bks[j][\"yes_ask\"])\n",
    "        _truth_i = next((j for j, c in enumerate(_bks) if _spec_contains(c[\"spec\"], _actual_f)), None)\n",
    "        if _truth_i is None:\n",
    "            continue\n",
    "\n",
    "        _records.append({\n",
    "            \"date\": _ev_ts,\n",
    "            \"agreement\": _model_i == _market_i,\n",
    "            \"model_i\": _model_i,\n",
    "            \"market_i\": _market_i,\n",
    "            \"truth_i\": _truth_i,\n",
    "        })\n",
    "\n",
    "    _df = pd.DataFrame(_records)\n",
    "    print(f\"Kalshi-bucket accuracy window: {_df['date'].min().date()} -> {_df['date'].max().date()}\")\n",
    "    print(f\"Total days evaluated:           {len(_df)}\")\n",
    "    print(f\"Agreement days (model_modal == market_modal):  {_df['agreement'].sum()}  ({_df['agreement'].mean():.1%})\")\n",
    "    print()\n",
    "\n",
    "    def _acc_block(label, sub):\n",
    "        if len(sub) == 0:\n",
    "            print(f\"{label}: no days\"); return\n",
    "        _m_t1 = (sub[\"truth_i\"] == sub[\"model_i\"]).mean()\n",
    "        _k_t1 = (sub[\"truth_i\"] == sub[\"market_i\"]).mean()\n",
    "        _m_t3 = (np.abs(sub[\"truth_i\"] - sub[\"model_i\"]) <= 1).mean()\n",
    "        _k_t3 = (np.abs(sub[\"truth_i\"] - sub[\"market_i\"]) <= 1).mean()\n",
    "        print(f\"{label}  (n={len(sub)})\")\n",
    "        print(f\"  {'':<32} {'Model':>10} {'Market':>10}\")\n",
    "        print(f\"  {'Top-1 (modal == truth bucket)':<32} {_m_t1:>10.1%} {_k_t1:>10.1%}\")\n",
    "        print(f\"  {'Top-3 (truth within +/-1 bucket)':<32} {_m_t3:>10.1%} {_k_t3:>10.1%}\")\n",
    "        print()\n",
    "\n",
    "    _acc_block(\"ALL days\", _df)\n",
    "    _acc_block(\"AGREEMENT days (model == market modal)\", _df[_df[\"agreement\"]])\n",
    "    _acc_block(\"DISAGREEMENT days\", _df[~_df[\"agreement\"]])\n",
    "\n",
    "    print(\"Production-relevant: on AGREEMENT days the joint modal's top-1 \"\n",
    "          \"and top-3 are exactly the wing+drop_lower_ask hit rate ceiling.\")"
]

new_md_cell = {
    "cell_type": "markdown",
    "metadata": {},
    "source": md_source,
}
new_code_cell = {
    "cell_type": "code",
    "execution_count": None,
    "metadata": {},
    "outputs": [],
    "source": code_source,
}

# Find the existing 10.2 code cell (cell 96 by index, but locate by content)
insert_after = None
for i, c in enumerate(nb["cells"]):
    src = "".join(c.get("source", []))
    if c["cell_type"] == "code" and "Bucket-accuracy diagnostic on the calibrated OOF" in src:
        insert_after = i
        break

if insert_after is None:
    print("ERROR: could not locate the existing 10.2 code cell.")
    raise SystemExit(1)

nb["cells"][insert_after + 1:insert_after + 1] = [new_md_cell, new_code_cell]
NB_PATH.write_text(json.dumps(nb, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
print(f"Inserted 2 new cells after cell {insert_after} (the existing 10.2 code cell).")
print(f"New cell count: {len(nb['cells'])}")
