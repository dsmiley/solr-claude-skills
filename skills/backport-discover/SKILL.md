---
name: backport-discover
description: Report-only backport triage for Apache Solr. Runs Stage 1 (discover pending commits, refresh GitHub PR metadata, resolve ambiguous milestones via prompts) then Stage 2 (select what's safely backportable right now to a target branch, gated by CI). Does not execute any backport -- use /backport (no args) for the same triage followed by execution. Use when the user wants to see what's ready to backport, resolve missing milestones, or check backport status without actually backporting anything, e.g. "/backport-discover", "what's ready to backport", "resolve missing milestones".
user_invocable: true
argument-hint: "[--committer <pattern>] [--all-committers] [--since <duration>] [--refresh] [--include-9x]"
---

# backport-discover

Report-only triage, composed of two scripts:

- **Stage 1** (`${CLAUDE_PLUGIN_ROOT}/skills/backport-discover/scripts/backport-discover.py`): walks the
  source branch, refreshes GitHub PR metadata for every pending commit, and surfaces the ones
  whose backport milestone is still ambiguous (no milestone on the PR, or no PR at all and no
  recorded override) so they can be resolved here.
- **Stage 2** (`${CLAUDE_PLUGIN_ROOT}/skills/backport-discover/scripts/backport-selector.py`): purely
  mechanical selection of what's safely backportable to one target branch right now, using
  whatever milestone info Stage 1 (or a real GitHub PR milestone) already resolved, gated by CI
  coverage.

These scripts are personal tooling, not part of the Solr repo -- they operate on your private
state file and git identity, so they live under this skill folder rather than `dev-tools/scripts/`.

This skill never executes a backport (no cherry-picking, no `/backport` handoff) -- it's for
triage and milestone resolution only. Once you have a `cherry_pick` list you're happy with, run
`/backport` (no args) to triage-and-execute in one go, or `/backport <sha> ...` to cherry-pick
specific commits directly.

Repo root is assumed to be the current working directory (the Solr checkout).
State file lives outside the repo at `~/.claude/solr-backport-state.json` -- it's
personal, not shared, and must already exist as valid JSON.

## Steps

### 1. Parse args

- `--committer <pattern>` (optional). Regex, case-insensitive, matched against git committer
  name/email or (when a PR exists) the GitHub merger login. Scopes which ambiguous entries
  Stage 1 surfaces for resolution.
- `--all-committers` (optional, default off). Without it, Stage 1 auto-scopes ambiguous entries
  to your own git `user.email` -- only you can judge whether your own direct-push commits or
  self-merged PRs need a milestone. Pass this to see everyone's ambiguous commits instead.
  Ignored if `--committer` is also given (explicit pattern wins).
- `--since <duration>` (default `1 month ago`)
- `--refresh` (force re-fetch of cached GitHub metadata instead of using the cache)
- `--include-9x` (optional, default off -- also triage `branch_9x` alongside `branch_10x`;
  without it, only `branch_10x` is checked, in both Stage 1's pending-computation and Stage 2's
  selection)

### 2. Ensure the state file exists

Check `~/.claude/solr-backport-state.json`. If missing, tell the user:

> State file not found. Initialize it once with: `echo '{}' > ~/.claude/solr-backport-state.json`

and stop. Do not create it automatically -- this file accumulates real decisions and
shouldn't spring into existence silently.

### 3. Run Stage 1 (discover + refresh)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/backport-discover/scripts/backport-discover.py \
  --since "<since>" \
  [--committer "<committer>"] \
  [--all-committers] \
  --state-file ~/.claude/solr-backport-state.json \
  --targets "branch_10x"  # or "branch_9x,branch_10x" if --include-9x \
  [--refresh]
```

Parse the JSON it prints: `ambiguous_pr`, `ambiguous_no_pr`, `committer_filter`. This also
silently refreshes GitHub PR metadata in the state file for every pending commit (not just the
ones matching the committer scope) -- Stage 2 depends on that regardless of who committed.

### 4. Resolve ambiguous milestones

Tell the user the committer scope in effect (`committer_filter`, or "everyone" if null).

For each entry in `ambiguous_pr` (has a PR number), `AskUserQuestion` (header "Milestone",
options `9.x` / `10.x` / `11.x` / "Skip for now"). `AskUserQuestion` caps each question at 4
options and 4 questions per call -- if you have more than 4 entries, ask in batches (e.g.
"(1/2)", "(2/2)"), listing `<short-sha> <subject>` per entry. If not skipped:

```bash
gh pr edit <pr_number> --milestone "<value>"
```

For each entry in `ambiguous_no_pr` (direct-push, no PR), same `AskUserQuestion` shape and
batching. If not skipped, write the decision into the state file instead of `gh pr edit` (there's
no PR to attach a milestone to):

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

"Skip for now" means don't record anything -- it stays ambiguous and gets re-proposed next run.

### 5. Run Stage 2 (select)

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/skills/backport-discover/scripts/backport-selector.py \
  --target "branch_10x" \
  --state-file ~/.claude/solr-backport-state.json
```

Run it again with `--target "branch_9x"` if `--include-9x` was passed. Parse each run's
JSON: `cherry_pick`, `pending_ci`, `needs_milestone`, `needs_10x_backport`, `ci_status`, `target`.

`backport-selector.py` chains branch_9x through branch_10x: a commit destined for branch_9x is
cherry-picked from branch_10x's own copy (not re-derived from main), since that copy may already
carry corrections made during the main -> branch_10x backport (e.g. a `@Deprecated(since=...)`
tag fixed from main's next-major version to 10x's next point release). See the "Chained sourcing"
note in `backport-selector.py`'s own docstring for the full rationale. This means a branch_9x
commit not yet on branch_10x can't be sourced at all yet -- `needs_10x_backport` holds those (see
step 6b).

### 6. CI status summary

Render `ci_status.jobs` as a table. For the `branch_10x` run this reflects `--source` (main). For
the `branch_9x` run (when `--include-9x`) it instead reflects **branch_10x's own CI**
(Solr-Test-10.x etc.), per the chained-sourcing note above -- the two Stage 2 runs are no longer
necessarily identical, so render each target's `ci_status` separately rather than assuming they
match:

```
JOB                         LATEST STATUS   FRONTIER (last green)
Solr-Lint-main              SUCCESS         595f760b  2026-07-09T00:14Z
Solr-Test-main              SUCCESS         595f760b  2026-07-09T00:14Z
Solr-TestIntegration-main   FAILURE         b8af94ab  2026-07-08T03:18Z  (build: <latest_build_url>)
docker-nightly              success         bfd7ff1a  2026-07-09T03:06Z
```

Only show the `(build: ...)` link when `latest_status` isn't a pass. State the overall
`high_water_mark` sha plainly: "CI high-water-mark: `<sha>` -- commits after this are
held back in Pending CI below."

### 6b. Needs-10x-backport list (branch_9x only, when --include-9x)

Same table shape as step 7, labeled "Needs branch_10x backport first -- not proposed for
branch_9x until landed there." Informational only: these need a separate `/backport-discover` (or
`/backport`) pass against `branch_10x` before they'll appear as branch_9x candidates.

### 7. Cherry-pick list (report only -- no execution)

For each target with `cherry_pick` entries, render a table:

```
DATE        AUTHOR          SUBJECT
2026-07-01  dsmiley         SOLR-18267: Add flat vector index with no HNSW
```

If empty for a target, say so. Do not offer `AskUserQuestion` selection or invoke `/backport`
here -- this skill only reports. Close with: "Run `/backport` to triage-and-execute these, or
`/backport <sha> ...` to cherry-pick specific ones directly."

### 8. Pending CI list

Same table shape as step 7, labeled clearly as held back: "Pending CI verification -- newer
than the high-water-mark, not yet actionable." Informational only.

### 9. Needs-milestone list (residual)

Anything still in `needs_milestone` after step 4 was either deferred ("Skip for now") or outside
this run's committer scope. Render: date, author, subject. Mention: rerun with `--all-committers`
to widen scope, or this skill again to answer the prompts.

### 10. Manual overrides (escape hatch)

Ask: "Any commits need a manual status override -- already ported under a different subject,
skip, or defer?" If yes, `AskUserQuestion` multiSelect over all commits seen this run (cherry-pick
+ pending-ci + needs-milestone lists combined), then for each selected commit ask which target
branch(es) it applies to and the status (`ported-different-subject` / `skip` / `deferred`), with
an optional free-text note. Write via the same inline `python3 -c` pattern as step 4, setting
`subjects["<subject>"]["targets"]["<branch>"] = {"status": "<status>"}` instead of
`milestone_override`.

## Notes

- This skill doesn't touch JIRA at all -- the reminder to resolve a JIRA issue only makes sense
  right after a cherry-pick actually completes all of a commit's required release lines, which
  only happens during `/backport`'s execution, not here.
- This skill never executes a backport. `/backport` (no args) runs this same Stage 1 + Stage 2
  triage internally and then falls into execution -- use that when you're ready to actually
  cherry-pick.
- State file auto-prunes terminal-status entries older than 120 days; that pruning happens
  inside `backport-discover.py` (Stage 1) itself, nothing to do here.
- CI coverage checking is `${CLAUDE_PLUGIN_ROOT}/skills/backport-discover/scripts/ci-status.py`, shelled out to by
  `backport-selector.py` (Stage 2). **This is the only CI gate in the whole flow** -- `/backport`
  itself doesn't check CI at all, so only hand it commits from a `cherry_pick` bucket (never
  `pending_ci`, never arbitrary SHAs without checking `ci-status.py` yourself first).
