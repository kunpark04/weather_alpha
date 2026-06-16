# Always-on deployment — Kalshi orderbook logger

The logger (`scripts/orderbook_logger.py`) must run on an **always-on host**. It
**cannot** survive your laptop shutting down — code can't fix a powered-off machine.
Run it on a cheap/free 24/7 box and let `systemd` keep it alive.

> **Actual production droplet:** `weather-alpha@137.184.128.37` (DigitalOcean NYC1, Ubuntu 24.04,
> 1 vCPU / 1 GB). The project lives in a **nested `~/weather-alpha` subdir at mode `0700`** (accessible
> only by root + the `weather-alpha` user): the git worktree root, `.venv`, `data/`, and `secrets/` all
> sit under `/home/weather-alpha/weather-alpha/`, while home root keeps only shell dotfiles + `.ssh/` +
> `.config/systemd/user/`. Checked out as a **lean sparse + `--filter=blob:none` partial** clone — only
> `weather_alpha config scripts deploy`, **never the full repo** (no notebooks/historical/archive/model
> artifacts; `tasks/lessons.md` L20). History: migrated user `fa` → `weather-alpha` (flat home) on
> 2026-06-08, then relocated flat → nested `~/weather-alpha` (0700) the same day
> (`deploy/migrate-to-weather-alpha.md`). The generic Option A/B recipes below assume a clone at
> `~/weather-alpha` — which now matches the real droplet; `/opt/weather-alpha` and user `fa` are
> illustrative only (`fa` no longer exists).

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
sudo useradd -r -m -d /opt/weather-alpha weather-alpha   # service user
sudo mkdir -p /opt/weather-alpha && sudo chown weather-alpha /opt/weather-alpha
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
- **Retrieve zips:** one-off — `rsync -av user@host:<clone>/data/orderbook/*/*.zip ./` (per city; or
  `.../data/orderbook/KXHIGHCHI/*.zip` for one). For an automated daily copy to your own machine, see
  **Pull settled-day zips to your local machine** just below (`scripts/pull-orderbook-zips.{sh,ps1}`).
- **Cadence:** edit `--interval` in the unit (`60` = 1-min, `300` = 5-min). 1-min is
  ~0.2 req/s, far under Kalshi's public limit.
- **No credentials required** — the logger uses Kalshi's public `/markets` +
  `/orderbook` endpoints. (Live *trading* is separate and needs the Kalshi RSA key.)

## Pull settled-day zips to your local machine (scheduled)
A laptop behind home NAT usually isn't reachable from the internet, so don't push from the
host — **pull from your machine** on a schedule. Two ready scripts fetch each new
`<series>/<date>.zip` down, with two modes:
- **copy** (default) — leave each zip on the host as a backup; already-pulled zips are skipped.
- **move** (`OB_MOVE=1`) — after a zip transfers *and is verified here*, delete it from the
  host so it ends up **only** locally. The delete fires only once the local copy is confirmed
  (rsync `--remove-source-files`; the `.ps1` checks the byte size matches), so a failed or
  partial pull never deletes the remote. Only `*.zip` is ever touched — the live raw `.jsonl`
  folders are never transferred or deleted, so the running logger is undisturbed.

Because the logger only zips a day *after* it stops being open, a day's zip appears the next
morning — the local copy runs ~1 day behind by design, and a missed run self-heals next time.

Prereq: passwordless SSH from your machine to the host (key-based; for an unattended job the
key must have no passphrase or live in an agent). Set the host via the `OB_HOST` env var (or
edit the config block atop the script). `OB_REMOTE_DIR` defaults to `weather-alpha/data/orderbook`
(home-relative — the droplet's nested `~/weather-alpha` layout); `OB_LOCAL_DIR`
defaults to `~/weather-alpha-data/orderbook`.

**Windows analysis box** — `scripts/pull-orderbook-zips.ps1` (native ssh/scp, no rsync):
```powershell
$env:OB_HOST = 'weather-alpha@your-logger-host'; $env:OB_MOVE = '1'   # OB_MOVE=1 -> delete host copy after verifying locally
pwsh -NoProfile -File scripts\pull-orderbook-zips.ps1                 # test once
# then a daily Scheduled Task. GOTCHA: Task Scheduler can't resolve a bare 'pwsh.exe' (-> 0x80070002
# FILE_NOT_FOUND), so use the STABLE WindowsApps app-alias path (versioned paths change every PS update).
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'; if(-not(Test-Path $pwsh)){$pwsh=(Get-Command pwsh).Source}
$ps1 = (Resolve-Path scripts\pull-orderbook-zips.ps1).Path
$act = New-ScheduledTaskAction  -Execute $pwsh -Argument "-NoProfile -File `"$ps1`"" -WorkingDirectory (Resolve-Path .).Path
$trg = New-ScheduledTaskTrigger -Daily -At 8am
# Laptop-friendly: wake to run; catch up on next wake if off/asleep; don't skip on battery.
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
[Environment]::SetEnvironmentVariable('OB_HOST','weather-alpha@your-logger-host','User')   # persist for the task
[Environment]::SetEnvironmentVariable('OB_MOVE','1','User')
Register-ScheduledTask -TaskName 'PullOrderbookZips' -Action $act -Trigger $trg -Settings $set -Force
```

**Linux / macOS analysis box** — `scripts/pull-orderbook-zips.sh` (rsync):
```bash
OB_HOST=weather-alpha@your-logger-host OB_MOVE=1 bash scripts/pull-orderbook-zips.sh   # test once
crontab -e        # then add a daily pull (move-mode; a missed run self-heals):
# 0 8 * * *  OB_HOST=weather-alpha@your-logger-host OB_MOVE=1 /path/to/scripts/pull-orderbook-zips.sh >> ~/.cache/ob-pull.log 2>&1
```

Drop `OB_MOVE` to fall back to copy-mode (host keeps every zip). In copy-mode, if the host's
disk later gets tight, prune already-pulled zips on the host with e.g.
`find ~/weather-alpha/data/orderbook -name '*.zip' -mtime +30 -delete`.

## Pull bot state — live_log history + positions snapshot (scheduled)
The forward-edge tracker (`scripts/forward_edge_tracker.py`, HANDOFF §1.7) reads
`data/live_log.parquet`. The bot writes that — and its Book `data/positions.json` — on the **droplet**,
so pull them down like the zips. `scripts/pull-state.ps1` (+ `scripts/merge_state.py`) handles the two
files' DIFFERENT natures safely (they are **not** inert settled archives like the zips):

- **`live_log.parquet`** is append-only and always-growing → the pull **rotates** it (atomic `mv` to a
  dated shard on the droplet; the bot recreates a fresh log on its next append, race-free), pulls the
  shard, byte-verifies it locally, then **deletes the shard from the droplet** — so the history lives
  **only** on local. `merge_state.py` folds shards into the canonical `data/live_log.parquet` (dedup by
  `run_utc,t_utc,ticker,mode`) and moves consumed shards to `data/state_archive/`.
- **`positions.json`** is the LIVE Book the running bot reads at startup → it is **COPIED** down
  (overwriting the local snapshot) and **left on the droplet**. Deleting the running bot's Book would
  break settlement / risk re-buys; it is tiny and bounded, so it stays. (This is the one deliberate
  exception to "delete the remote copy.")

Verification-before-delete: a shard is removed from the droplet only after its byte size is confirmed
locally, so a failed/partial pull never deletes the only copy.

**Closed-laptop safe.** A sleeping laptop can't *receive* data, so the droplet is the buffer: while the
laptop is closed the log keeps growing and the Book stays current; nothing is deleted until it is
verified locally. Register the task with `-StartWhenAvailable` so a run missed during sleep fires on the
next wake and catches up — no data lost over any closure length. (To also *wake* the box on schedule,
add `-WakeToRun` to the settings + enable wake timers in the power plan; usually unnecessary, since
catch-up-on-wake covers it.)

Prereq: the same passwordless SSH key the zip pull uses. Host via `WA_HOST` (falls back to `OB_HOST`);
`WA_REMOTE_PROJ` defaults to `weather-alpha` (= `~/weather-alpha`; nested layout), `WA_LOCAL_DATA` to the repo's `data/`.

**Raw market-data location.** Only the **orderbook depth** (raw weather *market* data — the bulk) is
relocated off the repo, via the `OB_LOCAL_DIR` User env var (the zip-pull task inherits it at run
time). **This machine** points it at `..\data\weather\orderbook`. The **bot logs**
(`live_log`/`positions`, written by `PullBotState`) deliberately stay in the repo's
`data\{paper,live}` — `WA_LOCAL_DATA`/`WA_DATA_DIR` are left unset, so `pull-state.ps1`,
`merge_state.py`, and `forward_edge_tracker.py` all default to the repo `data/`. (`data/backfill/`
also stays — `load_city` hardcodes the repo path.)

```powershell
$env:WA_HOST = 'weather-alpha@137.184.128.37'
pwsh -NoProfile -File scripts\pull-state.ps1            # test once (rotates+pulls, merges into data\)
# then a daily Scheduled Task. GOTCHAS (both bite): Task Scheduler can't resolve a bare 'pwsh.exe'
# (-> 0x80070002 FILE_NOT_FOUND), and the WindowsApps *versioned* path changes on every PS update --
# so use the STABLE app-alias path. Persist WA_HOST/WA_PYTHON at User scope so the unattended run
# inherits them (the task won't see your interactive shell's env or PATH).
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'   # stable across PS updates (Store install)
if (-not (Test-Path $pwsh)) { $pwsh = (Get-Command pwsh).Source }      # MSI-install fallback (C:\Program Files\PowerShell\7)
$ps1  = (Resolve-Path scripts\pull-state.ps1).Path
$repo = (Resolve-Path .).Path
[Environment]::SetEnvironmentVariable('WA_HOST','weather-alpha@137.184.128.37','User')
[Environment]::SetEnvironmentVariable('WA_PYTHON',(Get-Command python).Source,'User')
$act = New-ScheduledTaskAction  -Execute $pwsh -Argument "-NoProfile -File `"$ps1`"" -WorkingDirectory $repo
$trg = New-ScheduledTaskTrigger -Daily -At 8:10am
# Laptop-friendly: wake to run; catch up on next wake if it was off/hibernated; don't skip on battery.
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'PullBotState' -Action $act -Trigger $trg -Settings $set -Force
# verify: Get-ScheduledTaskInfo -TaskName PullBotState  ->  LastTaskResult 0 = success
```

After it runs, score the forward edge: `python scripts/forward_edge_tracker.py` (the `FWD-TRACK`
rollup; HANDOFF §1.7 / §7). Forward fires are **captured** immediately; they **score** once their
settled day lands in the truth tape — kept current by the `RefreshTruthTape` job below.

## Refresh the truth tape (scheduled)
The truth tape (`data\backfill`, a junction to `..\data\weather\backfill`) is what `load_city` reads
for settlement outcomes. Recent settled days come from Kalshi's **authenticated** live tier, and this
laptop has no key — so `scripts/refresh-truth-tape.ps1` does the fetch **on the droplet** (which has
`secrets/kalshi-rw.env`) and then moves the zips down with the **same zip/move/delete convention** as
the orderbook pull:

1. SSH → `backfill_cities.py --days N --series …` on the droplet (authed; resumable; writes
   `data/backfill/<series>/<date>.zip` there).
2. Pull those zips into the local tape and **delete them from the droplet** (byte-verify first), by
   reusing `pull-orderbook-zips.ps1` in move-mode → droplet stays lean.

Because the droplet copies are deleted, each run **re-fetches the `--days` window** (the cost of the
lean-droplet convention). Tune with `TAPE_DAYS` (default 7) / `TAPE_SERIES` (default all 20). The local
tape **accumulates**; only the droplet side is pruned.

```powershell
$env:WA_HOST = 'weather-alpha@137.184.128.37'
pwsh -NoProfile -File scripts\refresh-truth-tape.ps1            # test (TAPE_SERIES/TAPE_DAYS to scope)
# daily Scheduled Task (stable pwsh alias; laptop-friendly), AFTER the 8:00/8:10 pulls:
$pwsh = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\pwsh.exe'; if(-not(Test-Path $pwsh)){$pwsh=(Get-Command pwsh).Source}
$ps1 = (Resolve-Path scripts\refresh-truth-tape.ps1).Path
$act = New-ScheduledTaskAction  -Execute $pwsh -Argument "-NoProfile -File `"$ps1`"" -WorkingDirectory (Resolve-Path .).Path
$trg = New-ScheduledTaskTrigger -Daily -At 8:20am
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 2)
Register-ScheduledTask -TaskName 'RefreshTruthTape' -Action $act -Trigger $trg -Settings $set -Force
```

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
sudo -u weather-alpha python3 -m venv .venv
sudo -u weather-alpha .venv/bin/pip install -e .        # BASE deps only — NOT .[data] (no HRRR needed)
sudo -u weather-alpha sed -i 's/^mode: paper/mode: live/' config/weather_alpha.yaml
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
sudo chown -R weather-alpha /opt/weather-alpha/secrets
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
sudo apt update && sudo apt install -y python3-venv python3.12-venv python3-pip git rsync  # fresh Ubuntu 24.04: venv module + git aren't in the base image
git pull                                   # get the deploy artifacts (first time: clone — see Option B above)
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

---

# Directional algo — Rust LIVE engine + Python per-anchor PAPER shadow (2026-06-15)

Separate from the retired `market_wing` bots. Forward-tests the directional algo
(`tasks/late_night_directional_probe_2026-06-15.md` §6f) against **live asks + order-book depth**. Two
implementations of the **same executable Option-A rule** (favorite mid ∈ 0.93–0.95 → buy; daily cap 3
entries/market/event-date, first-come as tz anchors fire east→west, **no cross-city look-ahead**;
16.67% slot; P&L at the achievable fill):

- **LIVE = Rust** ([`rust/`](../rust/README.md), real-order-capable, **ships built but UNARMED**). Build a
  Linux binary in WSL (`powershell -File deploy/build-engine.ps1` → `rust/dist/wa-engine`), `scp` it to
  `weather-alpha@HOST:weather-alpha/bin/`, install `deploy/wa-engine.service` (ships `--paper`, disabled).
  **Arm later:** edit `--paper`→`--live`, ensure `secrets/kalshi-rw.env`, `systemctl --user enable --now wa-engine`.
  ⚠️ **Before arming, fix the orderbook parse** — `rust/crates/wa-kalshi/src/parse.rs` reads the stale
  `orderbook`/cents key, not the live `orderbook_fp`/`*_dollars`, so the armed engine sees an **empty depth
  book** (best-ask fallback only). The same bug was fixed in `scripts/directional_paper.py` 2026-06-16; the
  Rust parity tests pass against a stale fixture. See HANDOFF §7 `RUST-OB` / `tasks/lessons.md` L23.
- **PAPER = Python** (`scripts/directional_paper.py`, **no orders**) — **rebuilt to follow live behavior**:
  fires **per (timezone, market) at each city's local anchor** and fetches the **LIVE Kalshi book at that
  moment** (not the logged tape); logs LIVE `yes_ask` + depth ladder + intended-vs-fillable + VWAP/slippage;
  settles via Kalshi `settlement_value`. Output: `data/directional_paper/{high,low}_log.parquet` + `paper_state.json`.
- **Logger** (`orderbook-logger-user`) still logs **39 series** for the backtest tape (independent of the bots).

**Timers** — 11 one-shot `--user` units `weather-alpha-paper-cap@*` (high 17:00 / low 22:00 **local** in
ET/CT/MT/PT/AZ = 10 capture + 1 daily settle @ 14:00 UTC), generated + enabled by
`deploy/install-paper-timers.sh`. The OLD batch timers `directional-paper-{high,low}` (00:30/06:00 UTC)
are **retired**.
```bash
scp scripts/directional_paper.py scripts/paper_capture.sh weather-alpha@HOST:weather-alpha/scripts/
scp deploy/weather-alpha-paper-cap@.service weather-alpha@HOST:.config/systemd/user/
scp deploy/install-paper-timers.sh weather-alpha@HOST:weather-alpha/deploy/
ssh weather-alpha@HOST 'chmod +x ~/weather-alpha/scripts/paper_capture.sh \
   ~/weather-alpha/deploy/install-paper-timers.sh && bash ~/weather-alpha/deploy/install-paper-timers.sh'
```
- **Capture is MAX-DATA** ("better safe than sorry"): one row per city SCANNED at the anchor (not just
  picks), 46 fields — all raw quote fields (bid/ask/sizes, vol, vol_24h, open_interest, liquidity,
  status), the full `yes_book`/`no_book` ladders + whole-event `distribution` (JSON), runner-up,
  `sum_yes_ask`, `staleness_sec` (≈0 now — fetched live at the anchor), plus the pick's feasibility.
  `picked` (first-3 in-band, executable cap — no look-ahead) drives paper P&L; the rest are archival
  context; settlement records `win` for every row.
- **Daily local pull** (`scripts/pull-directional-paper.ps1`, Scheduled Task `PullDirectionalPaper`,
  **11:00 ET** — moved past the 14:00 UTC settle so the pull reflects the prior night's SETTLED P&L):
  COPIES `{high,low}_log.parquet` + `paper_state.json` down to the repo's `data/directional_paper/`
  (no rotation/delete — the droplet stays the canonical writer). Runs indefinitely; closed-laptop safe.
- **Nightly peek (no pull):** `ssh weather-alpha@HOST 'bash weather-alpha/scripts/paper_peek.sh'` —
  prints today's scanned/entered counts, the entries table (+ win/PnL once settled), and the bankroll.
