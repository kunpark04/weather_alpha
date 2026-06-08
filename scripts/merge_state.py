"""Merge pulled live_log shards into the canonical PER-MODE local logs (dedup), archive consumed shards.

The resident bots write isolated state per mode (config/{paper,live}.yaml):
  data/paper/live_log.parquet , data/live/live_log.parquet  (+ each mode's positions.json).
scripts/pull-state.ps1 rotates each and stages shards as:
  data/state_inbox/<mode>/live_log-<stamp>.parquet   -> merged into -> data/<mode>/live_log.parquet
(a shard directly under data/state_inbox/ with no <mode> subdir -> legacy data/live_log.parquet).
Consumed shards move to data/state_archive/<same relpath>. Idempotent; safe to run standalone.

Why shards: each <mode>/live_log.parquet is append-only and always-written, so the pull ROTATES it
(atomic mv -> shard; the bot recreates a fresh log) and folds the shards in here. positions.json is
COPIED by the pull (the live Book stays on the droplet), not merged here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pandas as pd

_ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ["WA_DATA_DIR"]) if os.environ.get("WA_DATA_DIR") else _ROOT / "data"
INBOX = DATA / "state_inbox"
ARCHIVE = DATA / "state_archive"
# A logged row is one (cycle, contract); these uniquely identify it across re-pulls / append races.
KEY = ["run_utc", "t_utc", "ticker", "mode"]


def _merge_group(canon: Path, shards: list[Path], archive_dir: Path) -> str:
    frames, before, consumed = [], 0, []
    if canon.exists():
        cur = pd.read_parquet(canon)
        before = len(cur)
        frames.append(cur)
    for s in shards:
        try:
            frames.append(pd.read_parquet(s))
            consumed.append(s)
        except Exception as e:                       # a partial/corrupt shard must not lose the canon
            print(f"  WARN unreadable shard {s.name}: {e} (left in inbox)")
    if not frames:
        return f"{canon.name}: nothing to do"

    df = pd.concat(frames, ignore_index=True, sort=False)
    n_concat = len(df)
    key = [c for c in KEY if c in df.columns] or list(df.columns)
    df = df.drop_duplicates(subset=key, keep="last").reset_index(drop=True)
    if "run_utc" in df.columns:
        df = df.sort_values("run_utc").reset_index(drop=True)

    canon.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(canon)
    archive_dir.mkdir(parents=True, exist_ok=True)
    for s in consumed:
        s.replace(archive_dir / s.name)
    rel = canon.relative_to(DATA)
    return f"{rel}: {before} existing + shards => {n_concat} concat -> {len(df)} dedup (key={key}); archived {len(consumed)}"


def main() -> int:
    if not INBOX.is_dir():
        print("merge_state: no state_inbox/ -> nothing to merge")
        return 0

    groups: dict[Path, tuple[list[Path], Path]] = {}
    for sub in sorted(p for p in INBOX.iterdir() if p.is_dir()):     # per-mode subdirs
        shards = sorted(sub.glob("live_log-*.parquet"))
        if shards:
            groups[DATA / sub.name / "live_log.parquet"] = (shards, ARCHIVE / sub.name)
    flat = sorted(INBOX.glob("live_log-*.parquet"))                  # legacy flat shards
    if flat:
        groups[DATA / "live_log.parquet"] = (flat, ARCHIVE)

    if not groups:
        print("merge_state: no shards in state_inbox -> nothing to merge")
        return 0
    for canon, (shards, arch) in groups.items():
        print("merge_state:", _merge_group(canon, shards, arch))
    return 0


if __name__ == "__main__":
    sys.exit(main())
