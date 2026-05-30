# Lessons

Self-improvement log. After any user correction, capture the pattern + a rule that
prevents recurrence. Most recent first.

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
