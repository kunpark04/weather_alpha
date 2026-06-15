#!/usr/bin/env bash
# Wrapper for the per-(tz,market) directional PAPER timers (systemd --user). The systemd instance
# name selects what to run:
#   "<market>-<tzcode>"  -> directional_paper.py --capture --market <M> --tz <IANA>   (live per-anchor)
#   "settle"             -> directional_paper.py --market both --settle               (book outcomes)
set -euo pipefail
DIR="$HOME/weather-alpha"
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"
cd "$DIR"
case "${1:-}" in
  settle)  exec "$PY" scripts/directional_paper.py --market both --settle ;;
  high-et) exec "$PY" scripts/directional_paper.py --capture --market high --tz America/New_York ;;
  low-et)  exec "$PY" scripts/directional_paper.py --capture --market low  --tz America/New_York ;;
  high-ct) exec "$PY" scripts/directional_paper.py --capture --market high --tz America/Chicago ;;
  low-ct)  exec "$PY" scripts/directional_paper.py --capture --market low  --tz America/Chicago ;;
  high-mt) exec "$PY" scripts/directional_paper.py --capture --market high --tz America/Denver ;;
  low-mt)  exec "$PY" scripts/directional_paper.py --capture --market low  --tz America/Denver ;;
  high-pt) exec "$PY" scripts/directional_paper.py --capture --market high --tz America/Los_Angeles ;;
  low-pt)  exec "$PY" scripts/directional_paper.py --capture --market low  --tz America/Los_Angeles ;;
  high-az) exec "$PY" scripts/directional_paper.py --capture --market high --tz America/Phoenix ;;
  low-az)  exec "$PY" scripts/directional_paper.py --capture --market low  --tz America/Phoenix ;;
  *) echo "unknown instance: ${1:-<none>}" >&2; exit 2 ;;
esac
