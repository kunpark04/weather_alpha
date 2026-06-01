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
- **Output:** `/opt/weather-alpha/data/orderbook/<SERIES>/<EVENT-DATE>/<ticker>.jsonl` while a
  day is trading; auto-zipped **per city** to `<SERIES>/<EVENT-DATE>.zip` once that event settles.
- **Retrieve zips:** `rsync -av user@vm:/opt/weather-alpha/data/orderbook/*/*.zip ./` (per city; or `.../data/orderbook/KXHIGHCHI/*.zip` for one)
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
- **Kill switch:** `python scripts/kill.py --config config/weather_alpha.yaml` (arm) /
  `--disarm` / `--status`. Halts the next run before any order; does not cancel orders already
  resting on Kalshi. `--config <the running bot's config>` is **required** (it locates that
  bot's `KILL_SWITCH` path) and loads with the live-cred check OFF, so it works from a credless
  shell — point it at the *exact* config the target bot runs.
- **Drawdown halt status/reset:** `python scripts/halt.py --status --config <cfg>` shows the
  per-city / account drawdown halts; `--reset [STATION]` clears them (a halted city still
  settles; it just stops opening new trades). Same `--config` requirement + credless load.
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

---

# Two-instance local setup — Chicago LIVE + Houston PAPER (`systemd --user`)

Two **independent** one-shot instances on a local Linux box (no root): Chicago in LIVE mode,
Houston in PAPER, each with **isolated state** (`data/chicago/` vs `data/houston/`), both firing
**right at 1:00 PM America/Chicago** (the anchor — `AccuracySec=1s`; the cycle runs in ~10 s). The single bot is
single-market/single-mode, so "Chicago live + Houston paper" = two processes, not one — and two
processes give clean live/paper segregation (no shared `positions.json`/bankroll to corrupt).
Units: `deploy/weather-alpha-{chicago,houston}.{service,timer}` (the `--user` variant of the
system template above).

```bash
# clone assumed at ~/weather-alpha (else edit WorkingDirectory in the .service files); venv at .venv
mkdir -p ~/weather-alpha/data/chicago ~/weather-alpha/data/houston \
         ~/weather-alpha/logs/chicago ~/weather-alpha/logs/houston

# Chicago needs the read-WRITE creds (Houston paper needs none):
mkdir -p -m 700 ~/weather-alpha/secrets
cat > ~/weather-alpha/secrets/kalshi-rw.env <<'EOF'
KALSHI_KEY_ID=<your read-write key id>
KALSHI_PRIVATE_KEY_PATH=/home/<you>/secrets/readwrite-private-key.pem
EOF
chmod 600 ~/weather-alpha/secrets/kalshi-rw.env

# install the user units + allow timers to run when logged out:
cp deploy/weather-alpha-chicago.service deploy/weather-alpha-chicago.timer \
   deploy/weather-alpha-houston.service deploy/weather-alpha-houston.timer ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload

# start PAPER now (safe); start LIVE only when ready for real orders:
systemctl --user enable --now weather-alpha-houston.timer
systemctl --user enable --now weather-alpha-chicago.timer      # <-- the go-live switch
systemctl --user list-timers 'weather-alpha-*'
journalctl --user -u weather-alpha-chicago.service -f          # watch a fire
```

- **`Persistent=false` on Chicago (LIVE)** — a missed run is skipped rather than caught up late
  (avoids an off-anchor REAL trade; the next run still settles any pending prior-day position).
  Houston uses `Persistent=true` (catch-up is harmless in paper).
- **Two kill switches:** `data/chicago/KILL_SWITCH` and `data/houston/KILL_SWITCH`
  (`python scripts/kill.py --config config/chicago_live.yaml`, likewise for Houston).
- **Manual test before scheduling:** `…/.venv/bin/python -m weather_alpha --headless --config config/houston_paper.yaml`
  (paper, safe); same with `chicago_live.yaml` + the RW env to go live by hand.

---

# Option B — one RESIDENT bot per mode, across ALL US timezones (multi-market)

The two-instance setup above is one process *per city*. The **resident multi-market bot** is
the other shape the engine supports: **one** long-running process per *mode* that trades a whole
**list** of cities (any US timezone) from a single `markets:` config, firing each city at ITS OWN
local 1 PM. One LIVE process for every live city + one PAPER process for every paper city. No
per-city timers — the process sleeps until the next city's anchor (capped at 5 min) and wakes to
trade it; live/paper stays cleanly separated because mode is a per-process property.

Configs: **`config/live.yaml`** (`mode: live`, a `markets:` list) and **`config/paper.yaml`**.
Add a city by appending one line to `markets:` — `{name, event_pattern, station, local_tz}` (use
the EXACT IANA tz, incl. no-DST `America/Phoenix`). Units:
`deploy/weather-alpha-{live,paper}.service` (resident `--loop`, `Restart=on-failure`).

```bash
mkdir -p ~/weather-alpha/data/live ~/weather-alpha/data/paper \
         ~/weather-alpha/logs/live ~/weather-alpha/logs/paper
# LIVE creds in secrets/kalshi-rw.env (same as the two-instance setup above).
cp deploy/weather-alpha-live.service deploy/weather-alpha-paper.service ~/.config/systemd/user/
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now weather-alpha-paper.service     # safe; resident paper bot
systemctl --user enable --now weather-alpha-live.service      # <-- go-live (REAL orders)
journalctl --user -u weather-alpha-live.service -f            # watch the ENTER/FILLED/SETTLED lines
```

- **Drawdown halts (latched):** a city stops at **25%** drawdown of the running account; the whole
  account stops at **50%**; both stay halted (settlement still runs) until you clear them:
  `python scripts/halt.py --status --config config/live.yaml` / `--reset [STATION]`.
- **NTP required** (`sudo timedatectl set-ntp true`) so each city's 1 PM is real-time accurate.
- **A vs B are interchangeable on the same engine** — A (per-city one-shot timers) is simplest for
  a single timezone; B (this resident bot) is the single "one bot for all live cities" the
  multi-market refactor was built for. **Don't run two LIVE shapes for the same city** (e.g. the
  live one-shot timer AND the live resident, both for Chicago) — they'd both place real orders and
  double-trade. A **LIVE process plus a PAPER process for the same city is fine** — paper places no
  real orders, so it's a harmless (and useful) shadow-test of the live decision.

---

# TL;DR — one-script local setup (bots + logger)

For a local Linux box, one script does ALL the one-time plumbing (venv + base install, isolated
state dirs, the LIVE creds template, and installing the three `systemd --user` units — rewriting
their paths to wherever you cloned). Then activation is two command lines.

```bash
git pull                                   # get the deploy artifacts
bash deploy/setup-local.sh                 # idempotent one-time setup; prints the activate lines
# then edit secrets/kalshi-rw.env (key id) + drop your RW PEM at secrets/readwrite-private-key.pem
# (only the LIVE bot needs creds; the paper bots + logger are keyless)

# activate — safe (no creds / no real orders): CHI+HOU paper bots + CHI+HOU depth logger
systemctl --user enable --now weather-alpha-paper.service orderbook-logger-user.service
# go live — REAL orders on Chicago at 1 PM CT (~$2.50/trade):
systemctl --user enable --now weather-alpha-live.service
```

Installs three `--user` units: `weather-alpha-live` (`config/live.yaml`), `weather-alpha-paper`
(`config/paper.yaml`, CHI+HOU shadow), `orderbook-logger-user` (KXHIGHCHI + KXHIGHTHOU depth).
Stop a service with `systemctl --user disable --now <unit>`; emergency-halt the live bot with
`python scripts/kill.py --config config/live.yaml`. `setup-local.sh` is idempotent — safe to re-run
after a `git pull`.
