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

Output (stdout): a single JSON object:
  cherry_pick     : [{sha, date, author, subject}, ...] oldest-first, CI-verified
  pending_ci      : same shape, but newer than the CI high-water-mark
  needs_milestone : [{sha, date, author, subject}, ...] still unresolved
                     (Stage 1's job, not this script's, to resolve)
  ci_status       : {high_water_mark, jobs: [...]} from ci-status.py
  target          : the --target this run was scoped to
"""

import argparse
import json

from backport_common import (
    TERMINAL_STATUSES,
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


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="main", help="Used only for the CI high-water-mark check")
    parser.add_argument("--target", default="branch_10x")
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--skip-ci-filter", action="store_true", help="Don't hold back commits newer than the CI high-water-mark")
    args = parser.parse_args()

    args.source = debase(args.source)
    args.target = debase(args.target)
    target_number = parse_branch_number(args.target)

    fetch_remotes([qualify(args.source), qualify(args.target)])

    state = load_state(args.state_file)
    target_subjects = branch_subjects(qualify(args.target))
    high_water_mark, ci_jobs = ci_high_water_mark(args.source, args.skip_ci_filter)

    cherry_pick = []
    pending_ci = []
    needs_milestone = []

    for subject, entry in state["subjects"].items():
        targets = entry.get("targets", {})
        if targets.get(args.target, {}).get("status") in TERMINAL_STATUSES:
            continue
        if subject in target_subjects:
            continue

        gh_info = entry.get("gh") or {}
        milestone_title = gh_info.get("milestone") or entry.get("milestone_override")
        m_num = milestone_number(milestone_title)

        sha = entry.get("sha_main")
        if not sha:
            continue

        if m_num is None:
            needs_milestone.append({
                "sha": sha,
                "date": entry.get("date"),
                "author": entry.get("author"),
                "subject": subject,
            })
            continue

        if m_num > target_number:
            continue  # not required for this target line at all

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

    print(json.dumps({
        "cherry_pick": cherry_pick,
        "pending_ci": pending_ci,
        "needs_milestone": needs_milestone,
        "ci_status": {"high_water_mark": high_water_mark, "jobs": ci_jobs},
        "target": args.target,
    }, indent=2))


if __name__ == "__main__":
    main()
