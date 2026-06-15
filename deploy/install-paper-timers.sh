#!/usr/bin/env bash
# Install the per-(tz,market) directional PAPER capture timers + a daily settle timer (systemd --user),
# replacing the OLD batch timers (directional-paper-{high,low} at 00:30/06:00 UTC). Idempotent.
#
# Prereqs (copied alongside this script before running): the template service
# `weather-alpha-paper-cap@.service` in ~/.config/systemd/user/, and scripts/paper_capture.sh
# (chmod +x) under ~/weather-alpha/scripts/. Run ON the droplet.
set -euo pipefail
U="$HOME/.config/systemd/user"
mkdir -p "$U"

declare -A TZ=( [et]=America/New_York [ct]=America/Chicago [mt]=America/Denver [pt]=America/Los_Angeles [az]=America/Phoenix )

# 10 capture timers: high @ 17:00 local, low @ 22:00 local, per timezone.
for code in et ct mt pt az; do
  for mk in high low; do
    [ "$mk" = high ] && t="17:00:00" || t="22:00:00"
    cat > "$U/weather-alpha-paper-cap@${mk}-${code}.timer" <<EOF
[Unit]
Description=WA paper ${mk} capture @ ${t} ${TZ[$code]}

[Timer]
OnCalendar=*-*-* ${t} ${TZ[$code]}
AccuracySec=20s
Persistent=false

[Install]
WantedBy=timers.target
EOF
  done
done

# daily settle (UTC morning; after the prior day's westmost low anchors elapse; idempotent + catch-up)
cat > "$U/weather-alpha-paper-cap@settle.timer" <<EOF
[Unit]
Description=WA paper settle (both markets) daily

[Timer]
OnCalendar=*-*-* 14:00:00 UTC
AccuracySec=2min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload

# retire the OLD batch timers (replaced by the per-tz capture timers + the settle timer)
systemctl --user disable --now directional-paper-high.timer directional-paper-low.timer 2>/dev/null || true

# enable the new timers
for code in et ct mt pt az; do
  for mk in high low; do
    systemctl --user enable --now "weather-alpha-paper-cap@${mk}-${code}.timer"
  done
done
systemctl --user enable --now "weather-alpha-paper-cap@settle.timer"

echo "=== weather-alpha paper timers ==="
systemctl --user list-timers --all | grep -E "NEXT|weather-alpha-paper" || true
