# Lessons

Self-improvement log. After any user correction, capture the pattern + a rule that
prevents recurrence. Most recent first.

---

## L7 — CRLF breaks bash piped from PowerShell; run remote bash single-line or strip CRs

**Problem.** Going live, I piped PowerShell here-strings (`@'...'@`) to remote `ssh … bash -s`.
The here-strings carry Windows **CRLF**, and PowerShell also appends a trailing newline to the
piped stream, so remote bash saw `\r`: it broke a `case` ("syntax error near unexpected token
`newline`"), appended `\r` to a path (`check_kalshi_auth.py\r` → "No such file"), and corrupted
the env file's value. Two failed iterations before I spotted the carriage returns.

**Solution / rules.**
- **Default to a single-line remote command:** `ssh host "cmd; cmd; cmd"` — no here-string, no
  multi-line, no CRLF to leak. Reliable. (Use single-quoted PowerShell wrapping so `$(...)`/`$VAR`
  evaluate on the remote, not locally.)
- **If a multi-line script is unavoidable, don't pipe a Windows here-string** — strip CRs
  (`$s.Replace("`r","")`) *and* expect a trailing newline on the pipe, or `scp` a real LF file and
  run that. Piping `@'...'@` raw will always leak `\r`.
- **A `\r` in the error is the tell.** "syntax error near unexpected token `newline`", or a path
  printed with a trailing char that shouldn't be there, = CRLF — fix the endings, don't chase the
  apparent auth/path/syntax bug.
- Same line-ending family as L6 (single-line commands for the *user* to paste), different
  direction (me → remote bash). When in doubt about line endings, go single-line.

**Why it matters.** Like L6, the surface error points everywhere except the real cause (line
endings), burning iterations on phantom auth/path bugs — costly mid-go-live.

---

## L6 — Hand the user single-line commands to paste; `\`-continuations break on paste

**Problem.** Twice this session a multi-line shell command I gave (a `\`-continued `git clone`,
then a `\`-continued `printf … >> authorized_keys`) failed when the user pasted it: the
backslash-newline arrived as backslash-**space**, so bash read `\ ` as an escaped literal space
and mangled the command — the clone got a bogus URL (` git@github.com…` → wrong SSH user →
"Permission denied"), and the printf detached from its `>>` redirect (key printed to screen,
never written). Both *looked* like auth/file failures but were pure paste-mangling.

**Solution / rules.**
- **A command for the user to paste must be ONE physical line** — no `\` continuations. A single
  long line pastes intact; a continued one frequently splits at the backslash.
- **Make it fail safe if it does split.** Avoid `… && rm` / `… >> file` tails that, detached,
  would execute or truncate something. For appending a key, a single `echo 'KEY' >> file` beats a
  multi-clause `printf … && chmod`.
- **Always pair a fragile write with a verify step** (`tail -2 authorized_keys`, `ssh -T`) so a
  silent no-op surfaces immediately, not two steps later.

**Why it matters.** A mangled-on-paste command throws a *misleading* error (auth/repo, not
syntax) that sends debugging down the wrong path — exactly what happened twice before we spotted
the backslash.

---

## L5 — Verify on REAL data/runtime, not just mocks; unit tests miss the parse-level crash

**Problem.** The 28 multi-market unit tests all passed, yet the first PAPER smoke run against
the *live* market crashed on Houston (`KXHIGHTHOU`): its bucket range lives in `yes_sub_title`
with `between`/`less`/`greater` strikes, not Chicago's `subtitle` — a shape no mock exercised.
Separately, the pre-live review found the kill switch wrote/read the *wrong file* because the
ops tool didn't take the running bot's `--config` — invisible to any single-process test.

**Solution / rules.**
- **Run the real thing once before declaring done.** Mocks encode *your* assumptions about the
  payload; a live fetch (or a real authenticated read) surfaces the field that's actually
  null/shaped-differently. A green unit suite is necessary, not sufficient.
- **Multi-instance / multi-market behavior needs a multi-instance check.** Anything keyed by
  config path, station, or tz (kill-switch file, positions snapshot, per-city state) can't be
  validated by a single in-process test — exercise ≥2 configs.
- **Parse the second example, not just the canonical one.** When generalizing a parser, feed it
  a genuinely different member of the set (Houston, not another Chicago) before trusting it.

**Why it matters.** "Tests pass" hid a crash that would have taken the live city down on day one;
the wrong-file kill switch would have failed exactly when it was needed most.

---

## L4 — Don't document a guarantee the code doesn't enforce

**Problem.** The refactor plan asserted the exposure cap "just works," but `execute()` never
actually checked `total_exposure_max_pct` — the cap existed only as a config field and a
sentence in a doc. The review caught it; the enforcement had to be *added* to match the claim.

**Solution / rules.**
- **A documented invariant must point at the line that enforces it.** Before writing "X is
  capped / X is enforced / X can't happen," grep for the check and confirm it runs on the live
  path. If there's no enforcing line, either add it or downgrade the doc to "intended, not yet
  enforced."
- **Config knob ≠ enforcement.** A field in `config.py` is an input, not a guarantee; the guard
  that reads it is the guarantee. Same trap as a CLI flag that's parsed but never used.
- Pairs with CLAUDE.md §4.5 ("don't hide filters") — the inverse failure: don't *advertise* a
  filter that isn't there.

**Why it matters.** A false safety claim is worse than a stated gap: the reader sizes up trusting
a cap that would never have fired.

---

## L3 — A fix can expose an adjacent bug; re-trace the whole path after changing it

**Problem.** Making `kill.py`/`halt.py` require `--config` (the correct fix for the wrong-file
bug, L5) then routed those ops tools to the *live* config — which tripped the live-credential
check in `load_config`, so the emergency-stop tools refused to run from a credless ops shell.
The first fix surfaced a second, opposite failure (now fixed via `require_live_creds=False`).

**Solution / rules.**
- **After a fix, walk the *new* code path end-to-end** — especially shared helpers the change now
  reaches (here, `load_config`'s cred gate). A fix that changes *which* inputs/paths are used can
  activate a guard that was dormant before.
- **Emergency / read-only tools must not depend on write-mode preconditions.** A kill switch or a
  status reader has to work in the most degraded shell (no creds, no network) — load with the
  strictest checks OFF.

**Why it matters.** The whole point of the kill switch is to work when things are bad; a fix that
makes it require live creds defeats it in exactly the credless incident-response scenario.

---

## L2 — Verify the actual failure path in code before calling something a "blocker"

**Problem.** Explaining a lean-server deployment risk, I asserted the headless `--loop`
would crash because **`refresh_data` (HRRR/herbie) raises**. Wrong: `fetch_live_hrrr`'s
`ImportError` is caught by `fetch_all_live`'s per-source try/except, so a missing `herbie`
is logged and skipped — no crash. The real hard-fail was **`load_bundle`**, which insists
all 5 weather parquets exist and raises `FileNotFoundError` on the absent HRRR file. I only
located it correctly after the user pushed back ("I'm confused, rephrase") and I read `data.py`.

**Solution / rules.**
- **Read the function before naming it the culprit.** When claiming "X crashes / X is the
  blocker," open X *and the layer around it* (its callers' try/except) first — a caught
  exception is not a crash.
- **Trace the failure to the exact line**, not the plausible-sounding one. The hard-fail was
  one layer away from where I pointed (`load_bundle`, not `refresh_data`).
- Same root as L1's "don't act on a phantom problem": verify, then assert.

**Why it matters.** A confidently-wrong mechanism sends the user (and the next agent) toward
the wrong fix — here, "install/trim herbie" instead of the real one, "don't make `load_bundle`
require model-only parquets in model-free."

---

## L1 — Don't fire large parallel tool batches; one failure cancels them all

**Problem.** I repeatedly sent ~10–25 tool calls in a single message. The harness
rule is: if *one* call in a parallel batch fails, every other call in that batch is
**cancelled**. So a single bad call produced a wall of `Cancelled: parallel tool
call …` results, wasted the whole batch, and looked alarming. This happened twice in
a row — and the second time was *immediately after* the user told me to stop.

Two triggers that set off the failing call:
1. **Fabricated identifier.** I cited a commit hash (`9b3a7e8`) that did not exist in
   the repo → `git` exited 128 → cancelled the batch.
2. **Read-before-exists.** I assumed `tasks/lessons.md` existed and queued ~15
   Read/Grep/Bash calls against it. It didn't exist → first call failed → batch
   cancelled.

**Solution / rules.**
- **Small batches.** Only parallelize calls that are genuinely independent AND each
  highly likely to succeed. When unsure, send **one call at a time**.
- **Probe before fan-out.** Verify a file exists (single `Glob` or `ls`) before
  queueing multiple reads/edits against it.
- **Never invent identifiers.** Commit hashes, paths, tickers — read them from
  `git log` / the filesystem first. Don't reconstruct from memory.
- **Don't act on a phantom problem.** Before "fixing" something (e.g. a
  "corrupted" config), confirm it's actually broken. In the prior turn every check
  showed the config was fine, yet I still rewrote it. Verify first, mutate second.

**Why it matters.** Cascading cancellations waste context, obscure the one real
error under ten fake ones, and erode trust — especially when repeated after a
correction.

---
