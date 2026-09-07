---
name: backport
description: Backport commits from a source branch (default main) to a release branch, with LLM-assisted merge-conflict resolution and a two-phase test runner (local compile+lint, crave for tests). Two modes -- give it explicit commit hashes to cherry-pick exactly those (no CI check, caller's responsibility); give it no args to triage-and-execute (discover pending commits, resolve ambiguous milestones, select what's CI-safe, then execute) in one run. Examples - "/backport deadbeef", "/backport deadbeef cafebabe", "/backport --from branch_10x abcd1234", "/backport".
user_invocable: true
argument-hint: "[--from <source-branch>] <commit-hash> [<commit-hash> ...]  |  [--committer <pattern>] [--all-committers] [--since <duration>] [--refresh]"
---

# backport

Drive the backport flow for Apache Solr commits from `main` to a release branch. Sister to the
manual `dev-tools/scripts/cherrypick.sh` -- same idea, but with conflict-resolution help and a
configurable test runner.

Repo root is assumed to be the current working directory and to be the Solr checkout.

## Mode selection

- **One or more commit hashes given** (optionally with `--from <source-branch>`) -> **Direct
  mode**: skip straight to cherry-picking exactly those commits. No CI check happens -- the
  caller is responsible for only handing this commits that are already CI-verified.
- **No commit hashes given** -> **Triage mode**: run discovery + milestone resolution + selection
  (the same two-stage pipeline as the `backport-discover` skill), then execute whatever
  cherry-pick candidates come out of it, per target branch.

---

## Triage mode (no args)

### T1. Parse args

- `--committer <pattern>` (optional). Regex, case-insensitive, matched against git committer
  name/email or (when a PR exists) the GitHub merger login. Scopes which ambiguous milestone
  entries get surfaced for resolution in T4.
- `--all-committers` (optional, default off). Without it, ambiguous-milestone resolution
  auto-scopes to your own git `user.email`. Ignored if `--committer` is also given.
- `--since <duration>` (default `1 month ago`)
- `--refresh` (force re-fetch of cached GitHub metadata instead of using the cache)

### T1b. Determine target branch

Infer from the current branch (`git branch --show-current`):
- `branch_9x` -> target = `branch_9x`.
- `main` or `branch_10x` -> target = `branch_10x`.
- anything else -> `AskUserQuestion` (header "Target branch", single-select, options
  `branch_10x` / `branch_9x` / "Other") to pick the target explicitly.

Targets are always bare branch names (`branch_10x`, not `apache/branch_10x`) -- that's the
convention used consistently across the state file, `backport-discover.py`/`backport-selector.py`
CLI args and JSON output, and this skill's own `<target>`/`apache/<target>` git-command templates
below. The `apache` remote qualification happens only at the point of an actual git operation
(`git fetch apache`, `git pull apache <target>`, `apache/<target>` as a ref) -- never store or
pass around a pre-qualified value, since a stray `apache/` prefix silently breaks state-file key
lookups in the discover/selector scripts (see the backport-subject-match-false-positives memory
for the incident this caused).

Wrap the result as a single-element `targets` list, used everywhere T3-T10 below reference
"target(s)". Each session backports to exactly one target branch; to backport to both 10x and
9x, run separate sessions from separate working trees.

### T2. Ensure the state file exists

Check `~/.claude/solr-backport-state.json`. If missing, tell the user:

> State file not found. Initialize it once with: `echo '{}' > ~/.claude/solr-backport-state.json`

and stop. Do not create it automatically.

### T3. Run Stage 1 (discover + refresh)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/backport-discover/scripts/backport-discover.py \
  --since "<since>" \
  [--committer "<committer>"] \
  [--all-committers] \
  --state-file ~/.claude/solr-backport-state.json \
  --targets "<comma-joined targets from T1b>" \
  [--refresh]
```

Parse the JSON it prints: `ambiguous_pr`, `ambiguous_no_pr`, `committer_filter`. This also
refreshes GitHub PR metadata in the state file for every pending commit, not just ones matching
the committer scope.

### T4. Resolve ambiguous milestones

Tell the user the committer scope in effect (`committer_filter`, or "everyone" if null).

For each entry in `ambiguous_pr`, `AskUserQuestion` (header "Milestone", options `9.x` / `10.x` /
`11.x` / "Skip for now"), batching in groups of 4 if there are more than 4 entries. If not
skipped:

```bash
gh pr edit <pr_number> --milestone "<value>"
```

For each entry in `ambiguous_no_pr` with a `SOLR-XXXXX`-prefixed subject, first check whether the
underlying JIRA issue already has an established milestone from a *different* commit/PR under the
same key -- a commit lacking its own PR (a direct push, or a small follow-up fix) doesn't mean the
JIRA issue itself is milestone-less, only that *this* commit has no PR to read one off of:

```bash
gh pr list --search "SOLR-XXXXX" --state merged --json number,title,milestone --limit 20
```

Also check the state file for other subjects sharing the same `SOLR-XXXXX` prefix with a
`gh.milestone` or `milestone_override` already set. If a milestone turns up either way, use it
directly (write it via the snippet below, skip the prompt) -- don't ask the user to guess a
milestone from the commit's own subject text alone (e.g. inferring `10.x` from "since = 10.1"):
that can contradict the ticket's real, already-established milestone and cause a premature
"ready to resolve" in T7 for a release line that hasn't actually been backported yet (see
[[feedback_backport_milestone_audit_before_asking]] for the incident this caused).

Otherwise, same shape as `ambiguous_pr`: `AskUserQuestion` (header "Milestone", options `9.x` /
`10.x` / `11.x` / "Skip for now"), batching in groups of 4 if there are more than 4 entries. If
not skipped, there's no PR to attach a milestone to, so write it into the state file instead:

```bash
python3 -c "
import json
from datetime import datetime, timezone
path = '$HOME/.claude/solr-backport-state.json'
with open(path) as f:
    state = json.load(f)
state['subjects']['<subject>']['milestone_override'] = '<value>'
state['subjects']['<subject>']['updated_at'] = datetime.now(timezone.utc).isoformat()
with open(path, 'w') as f:
    json.dump(state, f, indent=2, sort_keys=True)
    f.write('\n')
"
```

"Skip for now" records nothing -- it stays ambiguous and is re-proposed next run.

### T5. Run Stage 2 (select)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/backport-discover/scripts/backport-selector.py \
  --target "<target>" \
  --state-file ~/.claude/solr-backport-state.json
```

Run once per target in the T1b `targets` list. Parse each run's JSON: `cherry_pick`,
`pending_ci`, `needs_milestone`, `ci_status`, `target`.

### T6. CI status summary

Render `ci_status.jobs` as a table (from either Stage 2 run -- it reflects `--source`, not
`--target`, so it's identical both times):

```
JOB                         LATEST STATUS   FRONTIER (last green)
Solr-Lint-main              SUCCESS         595f760b  2026-07-09T00:14Z
Solr-Test-main              SUCCESS         595f760b  2026-07-09T00:14Z
Solr-TestIntegration-main   FAILURE         b8af94ab  2026-07-08T03:18Z  (build: <latest_build_url>)
docker-nightly              success         bfd7ff1a  2026-07-09T03:06Z
```

Only show the `(build: ...)` link when `latest_status` isn't a pass. State the `high_water_mark`
plainly: "CI high-water-mark: `<sha>` -- commits after this are held back in Pending CI below."

### T7. Cherry-pick selection and execution

For each target with `cherry_pick` entries, render a table:

```
DATE        AUTHOR          SUBJECT
2026-07-01  dsmiley         SOLR-18267: Add flat vector index with no HNSW
```

If all targets are empty, say so and skip to T8.

For each non-empty target, use `AskUserQuestion` (multiSelect, header "Backport to `<branch>`?")
listing `<short-sha> <subject>` per commit, batching in groups of 4 if there are more than 4
candidates. If the user dismisses without answering, stop and wait for direction rather than
assuming empty selections.

For each target with selections: jump to **Execution** below with `<target>` already known (skip
step D2's target-branch prompt) and the selected SHAs (oldest-first, as given) as the commit
list. After Execution's summary (step D6) completes for this batch, write each commit's result
back into the state file:

- `clean`, `resolved-by-llm`, `resolved-by-mergetool` -> status `backported`
- `already-applied` -> status `ported-different-subject`
- `not-applicable` -> status `skip` (final -- won't be re-proposed; milestone already adjusted
  inline in D4 if that option was chosen)
- `aborted`, `skipped` -> no change (stays pending, re-proposed next run) -- this is the Abort
  branch's cascade (user declined to resolve a real conflict and stopped the loop), distinct from
  `not-applicable`'s deliberate, final determination that the commit doesn't belong on this target

Write via an inline `python3 -c` snippet that does two things in one pass: writes each commit's
status, and -- for the ones that flip a `SOLR-XXXX`-prefixed subject to fully complete across
*every* release line its milestone requires -- collects those JIRA keys to hand to D7. This is
the only place completion is ever checked; it deliberately doesn't rescan the whole state file,
only the subjects just written this batch:

```python
import json, re
path = "$HOME/.claude/solr-backport-state.json"
with open(path) as f:
    state = json.load(f)

KNOWN_LINES = {"branch_9x": 9, "branch_10x": 10}
DONE_STATUSES = {"backported", "ported-different-subject"}
branch = "<target>"
now = "<current UTC ISO timestamp>"
updates = {"<subject1>": "<status1>", "<subject2>": "<status2>"}  # this batch only

ready_to_resolve = []
for subject, status in updates.items():
    entry = state["subjects"][subject]
    entry.setdefault("targets", {})[branch] = {"status": status}
    entry["updated_at"] = now

    m = re.match(r"^(SOLR-\d+)", subject)
    if not m or status not in DONE_STATUSES:
        continue
    gh = entry.get("gh") or {}
    milestone = gh.get("milestone") or entry.get("milestone_override")
    mm = re.match(r"^(\d+)\.x$", (milestone or "").strip())
    if not mm:
        continue
    required = {b for b, num in KNOWN_LINES.items() if num >= int(mm.group(1))}
    if all(entry.get("targets", {}).get(b, {}).get("status") in DONE_STATUSES for b in required):
        ready_to_resolve.append(m.group(1))

with open(path, "w") as f:
    json.dump(state, f, indent=2, sort_keys=True)
    f.write("\n")

print(json.dumps(ready_to_resolve))
```

Capture `ready_to_resolve` (a list of JIRA keys) for use in D7. Note this only checks
`KNOWN_LINES` -- if a required line was completed outside this tool's tracking (a manual
cherry-pick, or history predating this state file), it won't be detected and the key just won't
appear here; it's a one-shot check, not a recurring scan.

Then return here and continue with the next target (if T1b's list has more than one), then
proceed to T8.

### T8. Pending CI list

Same table shape as T7, labeled "Pending CI verification -- newer than the high-water-mark, not
yet actionable." Informational only, no `AskUserQuestion`.

### T9. Needs-milestone list (residual)

Anything still in `needs_milestone` after T4 was deferred ("Skip for now") or outside this run's
committer scope. Render: date, author, subject. Mention rerunning with `--all-committers` to
widen scope.

### T10. Manual overrides (escape hatch)

Ask: "Any commits need a manual status override -- already ported under a different subject,
skip, or defer?" If yes, `AskUserQuestion` multiSelect over all commits seen this run (cherry-pick
+ pending-ci + needs-milestone combined), then for each selected commit ask target branch(es) and
status (`ported-different-subject` / `skip` / `deferred`), optional free-text note. Write via the
same inline `python3 -c` pattern as T7.

---

## Direct mode (explicit commit hashes)

### D1. Parse args

- Argv: optional `--from <source-branch>` (default `main`), then one or more commit hashes
  (short or long). Empty hash list with no other args falls through to Triage mode instead (see
  Mode selection above) -- this step only applies once hashes are confirmed present.
- Source branch = value of `--from`, else `main`.
- Commits are assumed to be valid and already in oldest-first order.
- Record the current branch so it can be reported in the summary:
  ```bash
  git branch --show-current
  ```
- Run `git fetch apache` once so subsequent steps see fresh refs.

### D2. Determine target branch

Infer from the current branch recorded in D1:
- `main` or `branch_10x` -> target is `branch_10x`, no prompt.
- `branch_9x` -> target is `branch_9x`, no prompt.
- anything else -> `AskUserQuestion` with options: `branch_10x` (default/first), `branch_9x`,
  plus "Other" so the user can type any branch name. Header: "Target branch".

(Skipped entirely when arriving from Triage mode T7 -- target branch is already known there.)

After getting the answer, note that cherry-picks create new SHAs, so the original commit SHAs
from the source branch will never appear verbatim on the target branch. No automatic
deduplication is performed -- trust the user to know what they're backporting.

## Execution (used by both modes)

### D3. Checkout the target branch

```bash
git checkout <target>
git pull --ff-only apache <target>
```

If pull fails (not fast-forward, local divergence), stop and tell the user. Do not try to
resolve.

### D4. Cherry-pick loop

For each commit (oldest first), delegate to `cherrypick.sh` in auto mode:

```bash
dev-tools/scripts/cherrypick.sh -s -n -a -b <target> <sha>
```

Flags used:
- `-s` -- skip precommit tests (step D5 owns testing)
- `-n` -- skip git pull (step D3 already updated the branch)
- `-a` -- auto-abort and exit non-zero on any conflict instead of going interactive
- `-b <target>` -- single target branch

**Exit 0:** record this commit as `clean`. Continue to the next.

**Non-zero:** inspect the output for the phrase "is now empty". If present, the commit's changes
are already on the target branch (cherry-picked under a different SHA earlier). Run
`git cherry-pick --skip` (if a cherry-pick is still in progress) or do nothing, then record this
commit as `already-applied` and continue to the next.

Otherwise, `cherrypick.sh` detected a conflict and aborted cleanly. Re-apply the same commit
manually to re-enter the conflict state:
```bash
git cherry-pick -x <sha>
```
Confirm it's a real conflict (not some other failure) by checking for unmerged paths:
```bash
git diff --name-only --diff-filter=U
```
If none, treat as a hard failure: stop the skill with the error output.

**Before resolving anything, run three cheap checks on the conflict:**

1. **Already ported under a different PR?** If the subject has a `SOLR-XXXXX` key, grep the
   target branch's history for it:
   ```bash
   git log --oneline HEAD --grep "SOLR-XXXXX"
   ```
   The same fix is often landed on release branches via a *separate PR* (different `(#NNNN)`
   suffix, so subject-matching missed it). A telltale sign is a conflict where the HEAD side
   already contains the incoming feature's code. If found: `git cherry-pick --abort`, record as
   `ported-different-subject` (note the existing SHA/PR), and continue to the next commit. Do
   this check *before* reading any large conflicted file.

2. **Rename/content misdetection.** Git sometimes pairs an incoming file with an *unrelated*
   target file of similar content (shows as a modify/delete "directory was renamed" hint, or
   conflict markers labeled with two different file paths). Verify the incoming path actually
   exists on the target: `git cat-file -e apache/<target>:<path>`. If it doesn't, and the
   commit's real change to that file is irrelevant on the target (file/module never existed
   there), resolve by restoring the HEAD-side file byte-identical
   (`git checkout apache/<target> -- <path>`) or `git rm` the orphaned incoming file --
   don't merge the mismatched contents.

3. **Feature/API absent from target.** Sometimes a conflict isn't really conflicting content to
   merge -- it's evidence that the code/feature this commit's real change touches was introduced
   by an *earlier*, not-yet-backported commit, so the target doesn't have it at all. Signs: the
   incoming side of the conflict is a whole method/field with no HEAD-side counterpart (HEAD's
   side is empty), or the hunk references a constant/method/class that doesn't exist on target --
   confirm with `grep` or `git show apache/<target>:<path>`. If confirmed, this commit doesn't
   apply to the target -- don't force it in as a merge. Also check whether the commit's (or its
   PR's) milestone implies this target line was expected (state file's `gh.milestone` /
   `milestone_override` in Triage mode, or `gh pr view <pr_number> --json milestone` otherwise); a
   milestone that requires this line despite the feature being absent is itself probably wrong.

   `AskUserQuestion` (header "Not applicable", naming the commit/subject per above, and explaining
   what's missing and why) with:
   - **Skip (defer)** -- this commit doesn't apply here; record it as final for this target
     (outcome `not-applicable`, written as state `skip` -- won't be re-proposed) and leave the
     milestone as-is. Use this when the milestone is fine as it stands, or you're not confident
     enough to change it.
   - **Skip (change milestone)** -- same final recording, plus fix the milestone right now:
     extract the PR number from the subject's trailing `(#NNNN)` (or
     `gh pr list --search <sha> --state merged --json number` if absent), `AskUserQuestion` for
     the corrected value if it isn't obvious (header "Milestone", options `9.x` / `10.x` /
     `11.x`), then `gh pr edit <pr_number> --milestone "<value>"`. If a state-file entry exists
     for this subject, also set `entry["gh"]["milestone"]` to the new value in the same write (see
     T7) so this run's completion check sees it immediately rather than waiting on a future
     `--refresh`.
   - *(fallback, not primary)* **It does apply -- resolve normally** -- the determination was
     wrong; fall through to the standard conflict-resolution flow below.
   - *(fallback, not primary)* **Abort + stop** -- same as the standard abort branch.

   Either skip option continues the loop with the next commit -- unlike the Abort branch, this
   does not stop the run. Record this commit's outcome as `not-applicable` for D6's summary and
   T7's write-back, not `aborted`/`skipped` (those mean something different -- see T7).

Split the conflicted files into lockfiles (`*.lockfile`, e.g. `gradle.lockfile`) and everything
else. Lockfile conflicts are never resolved by merging their markers -- always resolve them by
regeneration (see "Lockfile conflicts" below). Only non-lockfile conflicts go through the
`AskUserQuestion` flow.

If there are non-lockfile conflicts, show the conflicted file list and use `AskUserQuestion`
(header "Conflict -- resolve"). Every conflict-related question -- this one and the "Apply this
resolution?" follow-up -- must name the commit's short SHA and subject up front in the question
text (e.g. "For 8bfa1e2d (SOLR-15601: RAT: add ASF license headers to more files): ..."), not just
the file path. The user is tracking multiple commits across a session and needs the subject to
know which change is being discussed before reading the rest of the question.
- **LLM resolve** -- Claude attempts the resolution.
- **Open mergetool** -- user resolves in a separate terminal.
- **Skip (final)** -- this commit doesn't belong on this target -- too large/divergent to
  responsibly auto-resolve or merge by hand right now (e.g. the conflict stems from the two
  branches' dependency sets having drifted, not from a mergeable content difference). Abort the
  cherry-pick, record the outcome as `not-applicable` (state `skip` -- final, won't be
  re-proposed), and continue the loop with the next commit. Distinct from **Abort + stop**: this
  only drops the one commit, the run keeps going.
- **Abort + stop** -- abort this commit and stop the loop entirely (earlier commits stay applied,
  this and all remaining commits stay `aborted`/`skipped` -- pending, re-proposed next run).

**LLM resolve branch:**
- For each unmerged file: Read it, parse `<<<<<<<` / `=======` / `>>>>>>>` blocks, decide a
  resolution by understanding the intent of both sides in context, Edit the file to remove
  markers and apply the chosen content.
- After all files are resolved (or if Claude judges any conflict too risky), show a unified diff:
  ```bash
  git diff
  ```
- `AskUserQuestion`: "Apply this resolution?" (naming the commit/subject per above) Yes / No (open
  mergetool instead).
- On Yes: `git add -u` then `GIT_EDITOR=: git cherry-pick --continue`. (`GIT_EDITOR=:` suppresses
  the commit-message editor; `git add -u` stages only modifications to tracked files, avoiding
  accidental inclusion of pre-existing untracked files.) Record as `resolved-by-llm`.
- On No: fall through to the mergetool branch.

**Mergetool branch:**
- `git mergetool` is interactive and cannot run inside Claude Code. Tell the user: "Open a
  separate terminal, `cd` to the repo root, run `git mergetool`, resolve all conflicts, save,
  then reply here to continue."
- Wait for the user to reply, then: `GIT_EDITOR=: git cherry-pick --continue`. Record as
  `resolved-by-mergetool`.

**Skip (final) branch:**
- `git cherry-pick --abort`. Record this commit as `not-applicable` (state `skip`, per T7).
  Continue the loop with the next commit -- skip D4a for this commit (no new code landed).

**Abort branch:**
- `git cherry-pick --abort`. Record this commit as `aborted` and all remaining as `skipped`. Jump
  to D6 (no tests).

**Lockfile conflicts (always, no `AskUserQuestion`):**
- Resolve any non-lockfile conflicts first (via the branches above), since lockfile regeneration
  depends on the build files already being in their final merged state.
- Clear the conflict markers in each conflicted lockfile with either side -- content doesn't
  matter, it's about to be regenerated:
  ```bash
  git checkout --ours -- <lockfile> && git add <lockfile>
  ```
  (repeat per conflicted lockfile)
- Regenerate all lockfiles fresh from the merged build files:
  ```bash
  gw resolveAndLockAll --write-locks
  ```
- Stage whatever the regeneration produced:
  ```bash
  git add -u
  ```
- Record this commit as `resolved-by-lockfile-regen` (combine with `resolved-by-llm` /
  `resolved-by-mergetool` in the summary if the same commit also had non-lockfile conflicts) and
  continue the cherry-pick.

### D4a. Per-commit compile check

After every commit lands in the loop -- `clean`, `resolved-by-llm`, `resolved-by-mergetool`, or
`resolved-by-lockfile-regen` (skip this for `already-applied`, which adds no new code) -- run a
quick compile check before moving to the next commit:

```bash
./gradlew --console=plain compileTestJava
```

This is cheap (no test execution, no lint) but catches a real failure mode that a clean git
merge does not: a commit whose diff applies without conflict markers, but references an API that
doesn't exist on the target branch (e.g. a Lucene method/constant added after the target branch's
pinned Lucene version, or any other cross-branch API drift). Because the merge itself is clean,
nothing in D4 surfaces this -- only actually compiling does. This is most likely across branches
with real architectural/dependency differences (e.g. `main` -> `branch_9x`), less likely between
adjacent, closely-tracked branches.

On failure: trace which cherry-picked commit introduced the problematic file (same technique as
Phase A below), fix it, then immediately squash the fix into that commit via `--fixup` +
autosquash (same commands as Phase A) -- don't defer the fix to the end of the loop. Re-run the
compile check to confirm the fix before continuing to the next commit. Fixing immediately, one
commit at a time, keeps the trace step unambiguous (the fixup target is always "the commit just
applied," never a guess across a growing pile of changes).

### D5. Tests (auto policy + two-phase runner)

Decide whether to run tests:
- If any commit was `resolved-by-llm`, `resolved-by-mergetool`, or `resolved-by-lockfile-regen`
  -> **run tests**.
- Otherwise -> `AskUserQuestion`: "All clean -- run tests anyway?" default No.

If tests are going to run, execute **two phases in order**:

**Phase A -- local compile + lint (always local):**
```bash
./gradlew --console=plain check -x test 2>&1 | tee build/gradle.log
```
On non-zero exit: fix the errors, then squash each fix into the correct commit using `--fixup` +
autosquash rebase.

**Before creating any `--fixup` commit**, trace which cherry-picked commit introduced the
problematic file:
```bash
git log --oneline <base-sha>..HEAD -- <path/to/file>
```
This identifies the correct `--fixup` target. Never guess based on thematic similarity -- always
trace first.

After fixing, autosquash in one step:
```bash
git commit --fixup <correct-sha> && GIT_SEQUENCE_EDITOR=: git rebase -i --autosquash <base-sha>
```
where `<base-sha>` is the commit just before the oldest cherry-picked commit (i.e., the
pre-existing tip of the target branch before the backport).

**Pre-existing failures (not caused by the backported commits):** Before tracing to a
`--fixup` target, rule out that the failure already exists on the target branch independent of
this backport -- e.g. a RAT/license-header gap, a pre-existing lint violation. Verify by
reproducing the same check against a clean copy of the target branch's upstream tip:
```bash
git worktree add <tmp-dir> apache/<target> --detach
cd <tmp-dir> && <same check command>
cd - && git worktree remove <tmp-dir> --force
```
If it fails there too, this is not a `--fixup` candidate -- squashing it into one of the
cherry-picked commits would misattribute a pre-existing gap to a commit that didn't cause it.
Instead, `AskUserQuestion` the user for how to proceed (skip the check / fix as a standalone
commit / stop). If fixing as a standalone commit, reference the PR whose *scope* the fix
completes -- not necessarily the PR that most recently touched the affected file -- with
`(follow-up to #NNNN)` at the end of the subject line. Check that the reference is still apt: if
a newer, not-yet-backported PR would obsolete the affected file/area entirely (e.g. it deletes
the file), that PR is not the one being completed -- prefer the PR whose scope the fix actually
completes instead.

**Phase B -- tests (crave):**
```bash
crave run -- ./gradlew --console=plain test
```
If `crave` isn't on `$PATH`, print a one-line hint and fall back to asking the user how to
proceed.

Crave mechanics: the build runs *remotely* -- the local CLI is just a launcher/follower, and
the remote job keeps running even if the local command is backgrounded or killed. Get the job
ID from `crave list` (shows "Your active jobs" plus a SUCCESSFUL/FAILED history); fetch a
finished job's full log with `crave getlog --jobID <N>` (save to a file, it's large). Crave
workers don't publish authenticated Develocity scans, so these runs are invisible to
Develocity.

On test failures: **triage before treating anything as a regression.** Check each failing test
against fleet history (solr-fucit-flaky-tests skill for fucit.org rates; solr-ci-triage skill
for Develocity per-branch history) and note the worker's health (wall time vs. a typical run;
suite-teardown `classMethod` timeouts are a classic contended-worker signature). Crave workers
vary widely -- an entire batch of failures can be environmental. If all failures are
known-flaky or environment-shaped, say so and offer a re-run; only unexplained failures block.
Leave the working tree as-is and tell the user how to inspect
(`git status`, `./gradlew :solr:core:test --tests …` for re-running a single test locally).

In the summary (D6), report tests as: `not run` / `passed (local+crave)` / `failed (phase A)` /
`failed (phase B)`.

### D6. Summary

Print a per-commit table:

```
COMMIT    SUBJECT                                 STATUS
abcd1234  Fix the foo when bar baz                clean
deadbeef  Refactor frobnicator                    resolved-by-llm
cafebabe  Update build deps                       skipped
```

Then:
- Target branch (with new local HEAD).
- Tests: `not run` / `passed (local+crave)` / `failed (phase A: local)` / `failed (phase B: crave)`.
- Original branch the user was on before the skill ran, and the command to return:
  ```bash
  git checkout <original-branch>
  ```
- Exact push command for the user to run themselves:
  ```bash
  git push apache <target>
  ```

D6 itself never pushes. The offer to run the push lives in D7's push gate.

In Triage mode, return to T7 after this to write results back into the state file.

### D7. JIRA resolution

Collect the JIRA keys, then -- after the commits are pushed and the user approves -- open each in
Firefox for the user to set its **Fix Version** and resolve.

**Direct mode:** no milestone/state tracking to check completeness against. Extract the `SOLR-\d+`
prefix from each cherry-picked commit subject and dedupe -- treat these as candidates (some may
already be resolved from when they landed on the source branch).

**Triage mode:** use the precise `ready_to_resolve` list T7 just computed -- issues now fully
backported across every release line their milestone requires.

If the list is non-empty, proceed through the gate and approval below.

**Push gate -- JIRA resolution must not happen before the commits are on the remote.** Check:

```bash
git fetch apache <target> -q
git rev-list --count apache/<target>..<target>
```

- **Non-zero** (commits not pushed): `AskUserQuestion` -- "<N> commit(s) not yet on
  `apache/<target>`. Push before JIRA resolution?" Options: `Push now` / `Already pushed -- re-check`
  / `Skip JIRA`.
  - `Push now`: run `git push apache <target>` (its Bash approval prompt is the user's
    confirmation). Then re-run the count -- zero continues; a push failure or still-non-zero is
    reported and D7 stops (open nothing).
  - `Already pushed -- re-check`: re-run `git fetch apache <target> -q` + the count (covers a push
    from another terminal). Same zero / non-zero handling.
  - `Skip JIRA`: stop D7.
- **Zero** (pushed): continue.

**Approval:** once the push gate passes, `AskUserQuestion` -- "Open <N> JIRA issue(s) in Firefox
for Fix Version / resolution?" Options: `Open` / `Skip`. For more than 5 keys, list them in the
question detail. This approval is always required; never open browser tabs without it.

**Open** (on approval): build one `https://issues.apache.org/jira/browse/<KEY>` URL per key and
open them in a single invocation (multiple URLs -> multiple tabs):

```bash
open -a Firefox https://issues.apache.org/jira/browse/SOLR-1234 https://issues.apache.org/jira/browse/SOLR-5678
```

Then print the reminder:

> Opened <N> JIRA issue(s). For each: confirm or add the **Fix Version** for the next release that
> will ship from `<target>`, then resolve as **Fixed** if it isn't already.

Triage mode prepends: "These are fully backported across every release line their milestone
requires: `<comma-separated keys>`."

If the list is empty, say nothing -- most batches won't complete every required line in one pass
(a 9.x milestone commit backported only to `branch_10x` this run still needs `branch_9x` before
it's truly done), so silence here is the normal case, not a sign something's wrong. In Direct
mode, empty means no `SOLR-` keys were present in the subjects.

In Direct mode, this is the last step. In Triage mode, this happens once per target after that
target's Execution pass, before moving on (T7) or to T8-T10.

## Notes

- This skill never edits `dev-tools/scripts/cherrypick.sh`; that script remains the manual
  fallback.
- No CI gate in Direct mode -- the caller must have already verified the commits are CI-covered.
  Triage mode's Stage 2 (`backport-selector.py`) does this itself via
  `${CLAUDE_PLUGIN_ROOT}/skills/backport-discover/scripts/ci-status.py` before anything reaches T7.
- `crave` availability is checked in D5 phase B; if missing, the skill falls back to asking the
  user how to proceed.
- Direct mode never touches the backport-discover state file (`~/.claude/solr-backport-state.json`)
  -- only Triage mode's T7 write-back does.
- The `backport-discover` skill runs Stages 1+2 alone (report-only, no execution) for a quick
  "what's backportable" check without committing to running Execution.
