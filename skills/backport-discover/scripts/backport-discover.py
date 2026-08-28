#!/usr/bin/env python3
"""
Stage 1 of the backport pipeline: discover Solr commits on a source branch
(default main) that are still pending against one or more release branches,
refresh their GitHub PR metadata, and surface the ones whose backport
milestone is still ambiguous so a human (or the calling skill) can resolve it.

"Pending" means: not yet present (by commit subject) on all --targets, and
not marked with a terminal status (skip / backported / ported-different-
subject) in the state file for the branches it's still missing from.

Ambiguity has two shapes:
  - has an associated PR, but that PR carries no milestone yet
  - has no associated PR (direct-push commit), and no milestone_override has
    been recorded for it in the state file yet

Resolving ambiguity (setting `gh pr edit --milestone`, or writing
milestone_override into the state file) is NOT done by this script — that's
the calling skill's job, via interactive prompting. This script only reports
what needs a decision, and unconditionally refreshes GitHub PR metadata
(milestone/pr_number/merged_by/labels) for *every* pending commit regardless
of committer, since Stage 2 (backport-selector.py) depends on that metadata
being fresh for everyone, not just whoever this run's committer scope covers.

Milestone convention (community, opt-in): a PR's milestone (9.x / 10.x / 11.x)
signals the lowest release line it must reach. Lower implies higher too, since
branch_9x's history passes through branch_10x's fork point: a 9.x milestone
requires backporting to both branch_9x and branch_10x; a 10.x milestone
requires only branch_10x; 11.x (or no milestone) requires nothing extra since
it's already on main.

State file: JSON, must already exist (create with `echo '{}' > <path>`).
Only PR metadata caching and terminal-status pruning are written by this
script; milestone/skip/deferred decisions are written by the caller (the
backport-discover skill), not this script.

Output (stdout): a single JSON object:
  ambiguous_pr     : [{sha, date, author, subject, pr_number, merged_by,
                       already_on, missing_from, furthest_backported_to}, ...]
  ambiguous_no_pr  : [{sha, date, author, subject, committer_name,
                       committer_email, already_on, missing_from,
                       furthest_backported_to}, ...]
  committer_filter : the effective committer regex used to scope the two
                      lists above, or null if unfiltered

--source/--targets are bare branch names (e.g. "main", "branch_10x"); the
upstream remote (see UPSTREAM_REMOTE in backport_common.py) is fetched
automatically before discovery runs.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone

from backport_common import (
    TERMINAL_STATUSES,
    branch_subjects,
    committer_matches,
    debase,
    fetch_remotes,
    gh_graphql_fetch,
    load_state,
    parse_branch_numbers,
    prune_state,
    qualify,
    recently_updated_milestone_prs,
    save_state,
    self_committer_pattern,
    source_commits,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", default="main")
    parser.add_argument("--targets", default="branch_9x,branch_10x")
    parser.add_argument("--since", default="1 month ago")
    parser.add_argument(
        "--committer",
        default=None,
        help="Regex filter, matched against git committer / GitHub merger. "
        "Default: auto-scoped to your own git user.email. Pass --all-committers "
        "to see everyone's ambiguous commits instead.",
    )
    parser.add_argument(
        "--all-committers",
        action="store_true",
        help="Don't scope to your own git identity; surface ambiguous commits from anyone.",
    )
    parser.add_argument("--state-file", required=True)
    parser.add_argument("--refresh", action="store_true", help="Force re-fetch of cached GitHub metadata")
    args = parser.parse_args()

    args.source = debase(args.source)
    target_branches = [debase(t) for t in args.targets.split(",")]
    branch_numbers = parse_branch_numbers(target_branches)

    fetch_remotes([qualify(args.source)] + [qualify(b) for b in target_branches])

    state = load_state(args.state_file)
    prune_state(state, target_branches)

    target_subject_sets = {b: branch_subjects(qualify(b)) for b in target_branches}
    commits = source_commits(qualify(args.source), args.since)
    now_iso = datetime.now(timezone.utc).isoformat()

    effective_committer = args.committer
    if effective_committer is None and not args.all_committers:
        effective_committer = self_committer_pattern()
        if effective_committer is None:
            print(
                "Warning: git config user.email not set; ambiguous commits won't be "
                "auto-scoped to you.",
                file=sys.stderr,
            )

    pending_entries = []
    shas_needing_gh = []
    for commit in commits:
        subject = commit["subject"]
        entry = state["subjects"].setdefault(subject, {})
        state_targets = entry.get("targets", {})

        pending = set()
        for b in target_branches:
            if subject in target_subject_sets[b]:
                continue
            status = state_targets.get(b, {}).get("status")
            if status in TERMINAL_STATUSES:
                continue
            pending.add(b)

        if not pending:
            continue

        entry["sha_main"] = commit["sha"]
        entry["author"] = commit["author"]
        entry["date"] = commit["date"]
        entry["committer_name"] = commit["committer_name"]
        entry["committer_email"] = commit["committer_email"]

        pending_entries.append((commit, subject, entry, pending))
        if args.refresh or entry.get("gh") is None:
            shas_needing_gh.append(commit["sha"])

    if not args.refresh:
        # Catch milestone drift on already-cached PRs cheaply, without a full
        # --refresh sweep: cross-check cached milestones against a small set of
        # recently-updated PRs per milestone line.
        drifted = recently_updated_milestone_prs()
        for commit, subject, entry, pending in pending_entries:
            gh_info = entry.get("gh")
            if not gh_info or gh_info.get("pr_number") is None:
                continue
            pr_number = gh_info["pr_number"]
            if pr_number in drifted and drifted[pr_number] != gh_info.get("milestone"):
                if commit["sha"] not in shas_needing_gh:
                    shas_needing_gh.append(commit["sha"])

    if shas_needing_gh:
        gh_results = gh_graphql_fetch(shas_needing_gh)
        for commit, subject, entry, pending in pending_entries:
            if commit["sha"] in gh_results:
                entry["gh"] = {**gh_results[commit["sha"]], "fetched_at": now_iso}

    ambiguous_pr = []
    ambiguous_no_pr = []

    for commit, subject, entry, pending in pending_entries:
        if entry.get("gh") is None:
            entry["gh"] = {"pr_number": None, "milestone": None, "labels": [], "merged_by": None, "fetched_at": now_iso}
        gh_info = entry["gh"]

        if not committer_matches(gh_info, commit, effective_committer):
            continue

        has_pr = gh_info.get("pr_number") is not None
        has_milestone = bool(gh_info.get("milestone"))
        has_override = bool(entry.get("milestone_override"))

        if (has_pr and has_milestone) or (not has_pr and has_override):
            continue  # already resolved, Stage 2's problem now

        already_on = set(target_branches) - pending
        furthest_num = min(
            (num for num, b in branch_numbers.items() if b in already_on),
            default=None,
        )
        common_fields = {
            "sha": commit["sha"],
            "date": commit["date"],
            "author": commit["author"],
            "subject": subject,
            "already_on": sorted(already_on),
            "missing_from": sorted(pending),
            "furthest_backported_to": branch_numbers[furthest_num] if furthest_num is not None else None,
        }

        if has_pr:
            ambiguous_pr.append({
                **common_fields,
                "pr_number": gh_info.get("pr_number"),
                "merged_by": gh_info.get("merged_by"),
            })
        else:
            ambiguous_no_pr.append({
                **common_fields,
                "committer_name": commit["committer_name"],
                "committer_email": commit["committer_email"],
            })

    save_state(args.state_file, state)

    print(json.dumps({
        "ambiguous_pr": ambiguous_pr,
        "ambiguous_no_pr": ambiguous_no_pr,
        "committer_filter": effective_committer,
    }, indent=2))


if __name__ == "__main__":
    main()
