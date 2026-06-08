#!/usr/bin/env bash
# Pull settled-day orderbook zips from the always-on logger host to THIS machine.
#
# Runs on your LOCAL / analysis box (Linux or macOS) — NOT the logger host. The logger
# writes <series>/<date>.zip on the host once a day settles; this fetches each new zip
# down to you. See deploy/README.md "Pull settled-day zips to your local machine".
#
# Modes:
#   default (copy)   leave each zip on the host as a backup; --ignore-existing skips zips
#                    already here, so a daily run just fetches what's new.
#   OB_MOVE set      MOVE: after a zip transfers AND is confirmed on this side, delete it
#                    from the host (rsync --remove-source-files), so it ends up ONLY here.
#                    rsync removes a source file only once it's verified received, so a
#                    failed/partial transfer never deletes the remote copy.
# Either mode touches only *.zip — the live raw .jsonl folders are never transferred or
# deleted, so the running logger is undisturbed. A missed run self-heals next time.
#
# Needs passwordless SSH from here to the host (key-based; for cron the key must have
# no passphrase or live in an ssh-agent). Configure via the vars below or env overrides.
set -euo pipefail

HOST="${OB_HOST:?set OB_HOST, e.g. weather-alpha@your-logger-host (or an ~/.ssh/config alias)}"
REMOTE_DIR="${OB_REMOTE_DIR:-weather-alpha/data/orderbook}"   # path on the host (rel = from $HOME; nested ~/weather-alpha layout)
LOCAL_DIR="${OB_LOCAL_DIR:-$HOME/weather-alpha-data/orderbook}"        # where to land them here

mkdir -p "$LOCAL_DIR"

# Transfer only *.zip, preserving the <series>/<date>.zip tree; skip the live raw .jsonl folders.
rsync_opts=(-az --include='*/' --include='*.zip' --exclude='*' -e ssh)
if [ -n "${OB_MOVE:-}" ]; then
  rsync_opts+=(--remove-source-files)   # MOVE: delete each zip from the host once it lands here OK
  mode="move (deleting host copies after verified transfer)"
else
  rsync_opts+=(--ignore-existing)       # COPY: keep host copies; never re-fetch one we already have
  mode="copy (keeping host copies)"
fi
rsync "${rsync_opts[@]}" "$HOST:$REMOTE_DIR/" "$LOCAL_DIR/"
echo "pull complete [$mode] -> $LOCAL_DIR"
