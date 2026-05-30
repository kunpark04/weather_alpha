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
