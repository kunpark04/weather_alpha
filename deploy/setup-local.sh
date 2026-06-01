#!/usr/bin/env bash
# One-time LOCAL setup for the weather-alpha bots + orderbook logger (systemd --user, no root).
# Idempotent — safe to re-run. After this, activation is just the `systemctl --user enable --now`
# lines printed at the end. Run from anywhere:   bash deploy/setup-local.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"
UNIT_DIR="$HOME/.config/systemd/user"
echo "weather-alpha local setup @ $ROOT"

# 1. venv + base install (skip if already present)
if [ ! -x .venv/bin/python ]; then
  echo "  - creating .venv + installing base deps (this can take a minute)..."
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -e .
else
  echo "  - .venv present, skipping install"
fi

# 2. isolated per-process state dirs (live / paper / logger never collide)
mkdir -p data/live data/paper data/orderbook logs/live logs/paper
echo "  - state dirs ready: data/{live,paper,orderbook}, logs/{live,paper}"

# 3. LIVE creds template (only the live bot needs these; paper + logger are keyless)
mkdir -p -m 700 secrets
if [ ! -f secrets/kalshi-rw.env ]; then
  cat > secrets/kalshi-rw.env <<EOF
# Read-WRITE Kalshi creds for the LIVE bot. Fill the key id + drop your PEM, then leave perms 600.
KALSHI_KEY_ID=PUT-YOUR-READ-WRITE-KEY-ID-HERE
KALSHI_PRIVATE_KEY_PATH=$ROOT/secrets/readwrite-private-key.pem
EOF
  chmod 600 secrets/kalshi-rw.env
  echo "  - wrote secrets/kalshi-rw.env  (EDIT it + place your RW PEM at secrets/readwrite-private-key.pem)"
else
  echo "  - secrets/kalshi-rw.env present, left as-is"
fi

# 4. install the systemd --user units, rewriting the unit paths to THIS clone location
mkdir -p "$UNIT_DIR"
for u in weather-alpha-live.service weather-alpha-paper.service orderbook-logger-user.service; do
  sed "s|%h/weather-alpha|$ROOT|g" "deploy/$u" > "$UNIT_DIR/$u"
done
loginctl enable-linger "$USER" >/dev/null 2>&1 || true
systemctl --user daemon-reload
echo "  - installed 3 --user units to $UNIT_DIR and daemon-reloaded"

cat <<'EOF'

--------------------------------------------------------------------------
Setup complete. ACTIVATE with these lines:

  # safe (no creds, no real orders) -- paper bots (CHI+HOU) + logger (CHI+HOU depth):
  systemctl --user enable --now weather-alpha-paper.service orderbook-logger-user.service

  # REAL MONEY (Chicago, ~$2.50/trade at 1 PM CT) -- only after editing secrets/kalshi-rw.env:
  systemctl --user enable --now weather-alpha-live.service

Watch:  journalctl --user -u weather-alpha-live.service -f
Stop:   systemctl --user disable --now weather-alpha-live.service
Kill:   .venv/bin/python scripts/kill.py --config config/live.yaml
NOTE:   ensure NTP is on once ->  sudo timedatectl set-ntp true
--------------------------------------------------------------------------
EOF
