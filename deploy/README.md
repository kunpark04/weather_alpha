# Always-on deployment — Kalshi orderbook logger

The logger (`scripts/orderbook_logger.py`) must run on an **always-on host**. It
**cannot** survive your laptop shutting down — code can't fix a powered-off machine.
Run it on a cheap/free 24/7 box and let `systemd` keep it alive.

## Why it stopped before
Two usual causes, both fixed here:
1. **No supervisor** — if the process crashed (or the terminal/SSH session closed),
   nothing restarted it. → `systemd Restart=always` (+ `WantedBy=multi-user.target`
   for reboots).
2. **An unhandled exception** killed the loop. → every poll cycle is now wrapped in
   try/except; a network/API error logs a warning and the loop continues.

## Where to host (no keys needed — public market data)
| Option | Cost | Notes |
|---|---|---|
| **Oracle Cloud Always Free** | free | Genuinely always-on ARM VM; best free option |
| **VPS** (Hetzner / DigitalOcean / Vultr / Linode) | ~$4–6/mo | Simplest, reliable |
| **Raspberry Pi / old PC at home** | free if owned | Leave it plugged in + on |

Avoid serverless/cron (e.g. GitHub Actions) — they're for scheduled jobs, not a
continuous logger.

## Setup (Linux)
```bash
# 1. provision a small Linux VM; then on it:
sudo useradd -r -m -d /opt/weather-alpha fa            # service user
sudo mkdir -p /opt/weather-alpha && sudo chown fa /opt/weather-alpha
# 2. copy the logger (just the one file is enough — stdlib only):
scp scripts/orderbook_logger.py  user@vm:/opt/weather-alpha/scripts/
# 3. python3 is preinstalled on most distros; tzdata is native on Linux.
# 4. install + start the service:
sudo cp deploy/orderbook-logger.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now orderbook-logger
# 5. watch it:
journalctl -u orderbook-logger -f
```

## Operate
- **Output:** `/opt/weather-alpha/data/orderbook/<EVENT-DATE>/<ticker>.jsonl` while a
  day is trading; auto-zipped to `<EVENT-DATE>.zip` once that event settles.
- **Retrieve zips:** `rsync -av user@vm:/opt/weather-alpha/data/orderbook/*.zip ./`
  (or a daily cron pushing to cloud storage — optional hardening).
- **Cadence:** edit `--interval` in the unit (`60` = 1-min, `300` = 5-min). 1-min is
  ~0.2 req/s, far under Kalshi's public limit.
- **No credentials required** — the logger uses Kalshi's public `/markets` +
  `/orderbook` endpoints. (Live *trading* is separate and needs the Kalshi RSA key.)

## Windows (if you must)
A Windows laptop still won't survive shutdown. If you have an always-on Windows box,
use Task Scheduler ("run whether logged on or not" + "restart on failure") or NSSM to
run it as a service, and `pip install tzdata`. A Linux VM is strongly preferred.

---

# Live bot — daily one-shot (systemd timer)

The trading bot is a **separate** concern from the logger above: it carries the Kalshi RSA
key and **places orders**. With the production `market_wing` config it is **model-free**, so
the host needs **no model artifacts and no HRRR/herbie stack**. Because it trades once a day
at the 1 PM CT anchor, it runs as a **one-shot fired by a timer** (not a resident loop): one
run trades today's wing, settles yesterday's position, then exits.

## Prereqs (same host as the logger, or its own box)
```bash
# get the code into /opt/weather-alpha (clone, or `git pull` if already there), then:
cd /opt/weather-alpha
sudo -u fa python3 -m venv .venv
sudo -u fa .venv/bin/pip install -e .        # BASE deps only — NOT .[data] (no HRRR needed)
sudo -u fa sed -i 's/^mode: paper/mode: live/' config/weather_alpha.yaml
```
No `data/model_v3_artifacts/` and no weather parquets are required — model-free pulls only
the CLI daily-high, on demand, when settling a prior day.

## Credentials — read-WRITE key for the bot
The bot reads `KALSHI_KEY_ID` + `KALSHI_PRIVATE_KEY_PATH` from its environment; systemd's
`EnvironmentFile=` injects them (the bot itself never parses `.env`). Use your **read-write**
key here — the bot places orders, so a read-only key would have them rejected.
```bash
# copy the read-write PEM up and hand it to the service user:
scp kalshi-rw.pem user@vm:/tmp/
sudo mkdir -p -m 700 /opt/weather-alpha/secrets
sudo mv /tmp/kalshi-rw.pem /opt/weather-alpha/secrets/
sudo tee /opt/weather-alpha/secrets/kalshi.env >/dev/null <<'EOF'
KALSHI_KEY_ID=<your read-write key id>
KALSHI_PRIVATE_KEY_PATH=/opt/weather-alpha/secrets/kalshi-rw.pem
EOF
sudo chown -R fa /opt/weather-alpha/secrets
sudo chmod 600 /opt/weather-alpha/secrets/kalshi.env /opt/weather-alpha/secrets/kalshi-rw.pem
```
Your **read-only** key is reserved for the monitor (built later): it gets its own
EnvironmentFile with the *same two variable names* pointing at the read-only key — no code
change, and only the bot ever holds write capability.

## Bring-up — test reads before arming writes
```bash
# read-only connectivity check (places NO orders) — run it with the READ-ONLY key first:
KALSHI_KEY_ID='<read-only id>' KALSHI_PRIVATE_KEY_PATH=/opt/weather-alpha/secrets/kalshi-ro.pem \
  /opt/weather-alpha/.venv/bin/python scripts/check_kalshi_auth.py     # -> PASS + balance/positions
# then install + enable the timer (which runs the bot with the read-write key):
sudo cp deploy/weather-alpha.service deploy/weather-alpha.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now weather-alpha.timer
systemctl list-timers weather-alpha.timer     # confirm next run ~13:05 America/Chicago
journalctl -u weather-alpha.service -f         # watch the preflight + cycle when it fires
```

## Operate
- **Kill switch:** `python scripts/kill.py` (arm) / `--disarm` / `--status`. Halts the next
  run before any order; does not cancel orders already resting on Kalshi.
- **Clock:** ensure NTP is on (`sudo timedatectl set-ntp true`) — Kalshi rejects skewed
  request signatures. The trade *date* is computed in America/Chicago regardless of host TZ.
- **One-shot vs loop:** this timer is the lean default. If you later want intraday fill
  retries or a live-dashboard host, run `--headless --loop` as a long-lived
  `Restart=on-failure` service instead (heavier; resident process).

## Plain cron (alternative to the timer)
If you'd rather use cron, put the timezone + a sourced env in the crontab (cron's env is
otherwise empty, so the creds must be loaded explicitly):
```cron
CRON_TZ=America/Chicago
5 13 * * *  set -a; . /opt/weather-alpha/secrets/kalshi.env; set +a; cd /opt/weather-alpha && .venv/bin/python -m weather_alpha --headless >> /opt/weather-alpha/logs/cron.log 2>&1
```
The systemd timer is preferred (TZ-aware, journal logs, catch-up via `Persistent=`).
