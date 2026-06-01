"""Re-render the market-accuracy tables from a saved parquet (no re-crawl).

    python scripts/summarize_accuracy.py [data/accuracy_all.parquet]

Prints the per-anchor x per-city grid (Top-1/2/3 + wing economics) and the 1PM summary.
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from market_accuracy_all import print_by_anchor, print_summary, HOURS  # noqa: E402

path = sys.argv[1] if len(sys.argv) > 1 else "data/accuracy_all.parquet"
df = pd.read_parquet(path)
print(f"loaded {path}: {len(df)} rows, {df['series'].nunique()} series, "
      f"{df['date'].nunique()} distinct dates\n")
print("#" * 60 + "\n#  PER-ANCHOR x PER-CITY GRID\n" + "#" * 60)
print_by_anchor(df, HOURS)
print("\n\n" + "#" * 60 + "\n#  1PM CROSS-CITY SUMMARY\n" + "#" * 60)
print_summary(df, HOURS)
