# Lessons

Self-improvement log. After any user correction, capture the pattern + a rule that
prevents recurrence. Most recent first.

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
