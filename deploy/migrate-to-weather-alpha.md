# Droplet migration: `fa` → `weather-alpha`, flattened layout

> **⚠️ SUPERSEDED 2026-06-08 (same day):** the flat home layout this runbook produces was immediately
> **relocated into a nested `~/weather-alpha` subdir at mode `0700`** (root + the `weather-alpha` user
> only). The Phase 0–3 sections below are the historical `fa`→`weather-alpha` *flat* migration; the
> **current** droplet layout is `/home/weather-alpha/weather-alpha/{weather_alpha, config, scripts,
> deploy, data, secrets, .venv, …}`. Current laptop pull env vars are `WA_REMOTE_PROJ=weather-alpha`
> and `OB_REMOTE_DIR=weather-alpha/data/orderbook` — **NOT** the `.` / `data/orderbook` shown in Phase 2.
> See the **"Relocation"** section at the bottom for exactly what changed.

**Goal.** Move the project from `/home/fa/projects/weather-alpha/` to a dedicated user whose **home IS
the project** (`/home/weather-alpha/{weather_alpha, config, scripts, deploy, data, secrets, .venv, …}`),
and decommission `fa`. One-user-per-project; the home dir becomes the write boundary.

**Method.** Clean **rebuild** as a new user (NOT in-place `mv`/`usermod`) — a venv hardcodes its absolute
path and a logged-in user can't be renamed from its own session, so a fresh build is safer. Cutover is a
short window (live bot down ~minutes); **open logs continue** via a stop→move-in-flight-files→start handoff
(single writer throughout — no double-running, nothing to dedupe). The live bot adopts its real
balance/positions from Kalshi on start (rule #11) — Kalshi is the source of truth — so there is no *local*
live state to lose (any open positions live on the exchange, not in the user's files).

**Actor tags:** `[ROOT]` = run as root on the droplet (DO console or a root SSH). `[wa]` = as the new
`weather-alpha` user. `[LOCAL]` = on the laptop (the agent does these). Do Phase 0 anytime; Phases 1–3
in one window, **outside the 1 PM-local anchor band** (≈17:00–22:00 UTC) so no city can miss a trade.

---

## Phase 0 — build the new user, services NOT started (no downtime)

```bash
# [ROOT] derive the exact remote + branch from fa's clone, then create the user
SRC=/home/fa/projects/weather-alpha
URL=$(sudo -u fa git -C "$SRC" remote get-url origin)
BR=$(sudo -u fa git -C "$SRC" rev-parse --abbrev-ref HEAD)
useradd -m -s /bin/bash weather-alpha
# (optional) match fa's sudo, or leave unprivileged for tighter isolation:
# usermod -aG sudo weather-alpha

# [ROOT] carry over SSH access (so the laptop/agent can reach the new user)
install -d -m700 -o weather-alpha -g weather-alpha /home/weather-alpha/.ssh
cp "$SRC"/../../.ssh/authorized_keys /home/weather-alpha/.ssh/ 2>/dev/null || cp /home/fa/.ssh/authorized_keys /home/weather-alpha/.ssh/
chown weather-alpha:weather-alpha /home/weather-alpha/.ssh/authorized_keys && chmod 600 /home/weather-alpha/.ssh/authorized_keys

# [ROOT] copy secrets (owner-only), then hand the rest to the new user
cp -r "$SRC/secrets" /home/weather-alpha/secrets
chown -R weather-alpha:weather-alpha /home/weather-alpha/secrets && chmod 700 /home/weather-alpha/secrets && chmod 600 /home/weather-alpha/secrets/*
loginctl enable-linger weather-alpha
```

```bash
# [wa] make HOME the repo worktree (flat). LEAN BY RULE (tasks/lessons.md L20): a PARTIAL clone
# (--filter=blob:none -> no blob history, .git ~1.3M) + SPARSE checkout of the RUNTIME-ONLY paths.
# NEVER a full clone -- the droplet must not carry notebooks/, historical/, archive/, docs/, or model
# artifacts (the model-free bot loads none of them). git init (not `git clone .`) because $HOME already
# has .bashrc/.profile.
cd ~
git init -q && git remote add origin "$URL"
git config core.sparseCheckout true
git sparse-checkout set weather_alpha config scripts deploy        # runtime only (deploy = unit reinstalls; drop for min)
git fetch --filter=blob:none -q origin "$BR"                       # PARTIAL: blob:none keeps .git tiny
git checkout -q "$BR"
python3 -m venv .venv && .venv/bin/pip -q install -U pip && .venv/bin/pip -q install -e .   # 2 GB swap already on the box
```

```bash
# [wa] install systemd --user units with HOME paths (rewrite /home/fa/projects/weather-alpha -> /home/weather-alpha)
install -d ~/.config/systemd/user
for u in weather-alpha-live weather-alpha-paper orderbook-logger-user; do
  sed 's#/home/fa/projects/weather-alpha#/home/weather-alpha#g' \
    /home/fa/.config/systemd/user/$u.service > ~/.config/systemd/user/$u.service
done
systemctl --user daemon-reload
# DO NOT start yet — Phase 1 starts them after the handoff.
```

> Verify the rewritten units have **no** `/home/fa/` left:
> `grep -Rn /home/fa ~/.config/systemd/user/ || echo OK`

---

## Phase 1 — cutover with open-log continuity (the short window)

```bash
# 1. [fa] stop fa's services -> flushes & closes the open jsonl + state files
systemctl --user stop weather-alpha-live weather-alpha-paper orderbook-logger-user

# 2. [ROOT] hand off the IN-FLIGHT open data so the new writers CONTINUE the same files
SRC=/home/fa/projects/weather-alpha; DST=/home/weather-alpha
for d in orderbook paper live; do
  install -d -o weather-alpha -g weather-alpha "$DST/data/$d"
  shopt -s dotglob nullglob; mv "$SRC/data/$d/"* "$DST/data/$d/" 2>/dev/null; shopt -u dotglob
done
mv "$SRC/data/scheduler_state.json" "$DST/data/" 2>/dev/null || true
chown -R weather-alpha:weather-alpha "$DST/data"

# 3. [wa] start the new services -> they re-open & APPEND the moved files; live bot re-adopts $balance/positions
systemctl --user start orderbook-logger-user weather-alpha-paper weather-alpha-live
```

**Validate (within the window):**
```bash
# [wa]
systemctl --user is-active weather-alpha-live weather-alpha-paper orderbook-logger-user        # all active
journalctl --user -u weather-alpha-live -n20 --no-pager | grep -i 'balance\|adopt\|preflight'  # adopts 23.56 / 0 positions
find ~/data/orderbook -name '*.jsonl' -newermt '-2 min' | head                                  # logger appending (fresh writes)
```

---

## Phase 2 — re-point the local pulls + validate end-to-end  `[LOCAL]`

```powershell
[Environment]::SetEnvironmentVariable('WA_HOST','weather-alpha@137.184.128.37','User')
[Environment]::SetEnvironmentVariable('OB_HOST','weather-alpha@137.184.128.37','User')
[Environment]::SetEnvironmentVariable('WA_REMOTE_PROJ','.','User')          # project IS the home dir now
[Environment]::SetEnvironmentVariable('OB_REMOTE_DIR','data/orderbook','User')
# pull-state / refresh-truth-tape already cd "$WA_REMOTE_PROJ" then use relative data/... -> '.' = home works.
pwsh -NoProfile -File scripts\pull-orderbook-zips.ps1   # smoke-test against the new user
pwsh -NoProfile -File scripts\pull-state.ps1
```
> The 3 scheduled tasks inherit these env vars on their next run; no re-registration needed.

---

## Phase 3 — retire `fa`  `[ROOT]` (only after Phase 2 validates)

```bash
sudo -u fa systemctl --user disable --now weather-alpha-live weather-alpha-paper orderbook-logger-user 2>/dev/null || true
loginctl disable-linger fa
# leave fa parked for a few days as a rollback, then when satisfied:
# deluser --remove-home fa     # (irreversible — only after you're sure)
```

## Rollback (if Phase 1 validation fails)
```bash
# [wa] stop new; [ROOT] move the in-flight data back; [fa] restart fa's services
systemctl --user stop weather-alpha-live weather-alpha-paper orderbook-logger-user
# ROOT: mv /home/weather-alpha/data/{orderbook,paper,live}/* back under /home/fa/projects/weather-alpha/data/...
sudo -u fa systemctl --user start orderbook-logger-user weather-alpha-paper weather-alpha-live
```

## Notes
- **Live-trading safety:** only ever ONE live bot touches the Kalshi account. Phase 0 never starts services;
  Phase 1 starts the new ones only after fa's are stopped. Never run both.
- **Secrets:** `kalshi-rw.env` uses a relative `KALSHI_PRIVATE_KEY_PATH` (`secrets/…`) so it survives the
  move; if it were absolute, fix it in `/home/weather-alpha/secrets/kalshi-rw.env`.
- **Truth tape / orderbook on the laptop** are unaffected (they live under `..\data\weather\`); only the
  pull source (`WA_HOST`/`WA_REMOTE_PROJ`) changes.

---

## Relocation: flat → nested `~/weather-alpha` (0700) — 2026-06-08

Right after the flat migration above, the project was moved out of `$HOME` into a dedicated
**`~/weather-alpha` subdir, `chmod 700`** (accessible only by root + the `weather-alpha` user). The
shared droplet hosts other project-users, so a `0700` subdir is the explicit read/write boundary —
independent of the `0775` home. Done in one window with **~7 s** of service downtime, with no LIVE trade
at risk (Chicago's 1 PM-CT anchor + 60-min window had already closed for the day).

1. `[wa]` stop the 3 `systemd --user` services (`weather-alpha-live`, `weather-alpha-paper`,
   `orderbook-logger-user`).
2. `[wa]` `mkdir ~/weather-alpha`; `mv` every project item (`.git .venv weather_alpha config scripts
   deploy data secrets logs`, docs, `pyproject.toml`, …) into it — **keeping** `.ssh/`,
   `.config/systemd/user/`, and shell dotfiles in `$HOME` (same filesystem → `mv` is instant).
3. `[wa]` `chmod 700 ~/weather-alpha`.
4. `[wa]` fix the **absolute** `KALSHI_PRIVATE_KEY_PATH` inside `secrets/kalshi-rw.env`
   (`/home/weather-alpha/secrets/…` → `/home/weather-alpha/weather-alpha/secrets/…`) — it is NOT
   relative, despite the Phase 0 note above; the bot loses its key without this.
5. `[wa]` re-point the venv editable install **without touching deps** (preserve the exact live
   versions — no drift on the money path): `~/weather-alpha/.venv/bin/python -m pip install -e . --no-deps`;
   validate `import weather_alpha`. (A moved venv works in place — `pyvenv.cfg` travels with it — only the
   editable finder needed re-pointing.)
6. `[wa]` rewrite the 3 unit files' paths and reload:
   `sed -i 's#/home/weather-alpha#/home/weather-alpha/weather-alpha#g' ~/.config/systemd/user/*.service`
   → `systemctl --user daemon-reload` → restart → confirm all `active`.
7. `[LOCAL]` re-point pulls (User scope): `WA_REMOTE_PROJ=weather-alpha`,
   `OB_REMOTE_DIR=weather-alpha/data/orderbook` (the 3 scheduled tasks inherit these on next run). The
   script **defaults** were also updated to the nested values, so a fresh checkout needs no env override.

**Rollback:** `mv ~/weather-alpha/* back to $HOME`, revert the unit `sed`, restore the absolute key path,
re-point the laptop env vars to `.` / `data/orderbook`.
