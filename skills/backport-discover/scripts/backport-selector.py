#!/usr/bin/env python3
"""
Stage 2 of the backport pipeline: given a state file whose commits already
carry resolved milestone info (real GitHub PR milestone, or a manually
recorded milestone_override for PR-less commits -- both produced by Stage 1,
backport-discover.py), select which are safely backportable right now to a
single --target release branch.

Purely mechanical: no GitHub API calls, no committer filtering, no
interactivity, and no git-log walk of the source branch -- it only reads what
Stage 1 already wrote into the state file, checks the current tip of
--target, and gates on CI freshness (ci-status.py's high-water-mark for
--source, unless --skip-ci-filter is passed).

Milestone convention: a milestone of N.x requires the commit reach every
release line numbered N or higher (branch_9x's history passes through
branch_10x's fork point, so a 9.x milestone requires branch_10x too). Here
that collapses to a single comparison against --target's own line number.

JIRA resolution is not this script's concern: it's handled by the /backport
skill itself, right after a cherry-pick actually completes all of a commit's
required release lines -- not reconstructed retroactively from this state
file, which can't reliably distinguish "done" from "never tracked".

Chained sourcing: branch_9x is downstream of branch_10x in the real backport
chain (main -> branch_10x -> branch_9x), so a commit destined for branch_9x
should be cherry-picked from branch_10x's own (possibly already-adapted)
copy, not re-derived from main -- main's copy can carry annotations/APIs
(e.g. a `@Deprecated(since=...)` tag for main's own next-major version) that
were already corrected once during the main -> branch_10x backport, and
re-deriving from main risks silently reintroducing what that correction
fixed. This is hardcoded to the current two-release-line reality
(CHERRY_PICK_SOURCE_OVERRIDE below); revisit when the branch lineup rolls
forward. When --target is branch_9x, this also switches the CI-readiness
gate to branch_10x's own high-water-mark instead of --source's (main's),
since what's being verified is "has branch_10x's own copy passed CI on the
10x line", not main's.

Output (stdout): a single JSON object:
  cherry_pick        : [{sha, date, author, subject}, ...] oldest-first,
                        CI-verified (sha is on the chained branch -- see
                        above -- when target has one, else on --source)
  pending_ci         : same shape, but newer than the CI high-water-mark
  needs_milestone    : [{sha, date, author, subject}, ...] still unresolved
                        (Stage 1's job, not this script's, to resolve)
  needs_10x_backport : [{sha, date, author, subject}, ...] milestone requires
                        branch_9x, but the commit hasn't reached branch_10x
                        yet -- sha is --source's (main's) copy, since no
                        branch_10x copy exists to cherry-pick from. Land it
                        on branch_10x first; only ever populated when
                        --target is branch_9x.
  ci_status          : {high_water_mark, jobs: [...]} from ci-status.py,
                        for whichever branch actually gated cherry_pick
                        above (branch_10x when chained, else --source)
  target             : the --target this run was scoped to
"""

import argparse
import json

from backport_common import (
    TERMINAL_STATUSES,
    branch_subject_shas,
    branch_subjects,
    ci_high_water_mark,
    debase,
    fetch_remotes,
    is_ancestor,
    load_state,
    milestone_number,
    parse_branch_number,
    qualify,
    save_state,
)

# branch_9x's real cherry-pick source is branch_10x, not main -- see the
# "Chained sourcing" note in the module docstring. Hardcoded to today's
# two-release-line reality; add an entry here if a future branch rolls
# through an equivalent chain.
CHERRY_PICK_SOURCE_OVERRIDE = {"branch_9x": "branch_10x"}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="main", help="Used for the CI high-water-mark check and as the cherry-pick source, unless --target chains through an intermediate branch (see CHERRY_PICK_SOURCE_OVERRIDE)")
    parser.add_argument("--target", default="branch_10x")
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--skip-ci-filter", action="store_true", help="Don't hold back commits newer than the CI high-water-mark")
    args = parser.parse_args()

    args.source = debase(args.source)
    args.target = debase(args.target)
    target_number = parse_branch_number(args.target)

    chain_branch = CHERRY_PICK_SOURCE_OVERRIDE.get(args.target)
    ci_source = chain_branch or args.source

    fetch_remotes(
        [qualify(args.source), qualify(args.target)]
        + ([qualify(chain_branch)] if chain_branch else [])
    )

    state = load_state(args.state_file)
    target_subjects = branch_subjects(qualify(args.target))
    chain_subject_shas = branch_subject_shas(qualify(chain_branch)) if chain_branch else None
    high_water_mark, ci_jobs = ci_high_water_mark(ci_source, args.skip_ci_filter)

    cherry_pick = []
    pending_ci = []
    needs_milestone = []
    needs_10x_backport = []

    for subject, entry in state["subjects"].items():
        targets = entry.get("targets", {})
        if targets.get(args.target, {}).get("status") in TERMINAL_STATUSES:
            continue
        if subject in target_subjects:
            continue

        gh_info = entry.get("gh") or {}
        milestone_title = gh_info.get("milestone") or entry.get("milestone_override")
        m_num = milestone_number(milestone_title)

        main_sha = entry.get("sha_main")
        if not main_sha:
            continue

        if m_num is None:
            needs_milestone.append({
                "sha": main_sha,
                "date": entry.get("date"),
                "author": entry.get("author"),
                "subject": subject,
            })
            continue

        if m_num > target_number:
            continue  # not required for this target line at all

        if chain_subject_shas is not None:
            chained_sha = chain_subject_shas.get(subject)
            if chained_sha is None:
                needs_10x_backport.append({
                    "sha": main_sha,
                    "date": entry.get("date"),
                    "author": entry.get("author"),
                    "subject": subject,
                })
                continue
            sha = chained_sha
        else:
            sha = main_sha

        covered = (
            high_water_mark is None
            or sha == high_water_mark
            or is_ancestor(sha, high_water_mark)
        )
        bucket = cherry_pick if covered else pending_ci
        bucket.append({
            "sha": sha,
            "date": entry.get("date"),
            "author": entry.get("author"),
            "subject": subject,
        })

    save_state(args.state_file, state)

    # state["subjects"] is alphabetically sorted (save_state uses sort_keys),
    # not chronological -- re-sort to the oldest-first contract callers expect.
    cherry_pick.sort(key=lambda c: c["date"])
    pending_ci.sort(key=lambda c: c["date"])
    needs_milestone.sort(key=lambda c: c["date"])
    needs_10x_backport.sort(key=lambda c: c["date"])

    print(json.dumps({
        "cherry_pick": cherry_pick,
        "pending_ci": pending_ci,
        "needs_milestone": needs_milestone,
        "needs_10x_backport": needs_10x_backport,
        "ci_status": {"high_water_mark": high_water_mark, "jobs": ci_jobs},
        "target": args.target,
    }, indent=2))


if __name__ == "__main__":
    main()
