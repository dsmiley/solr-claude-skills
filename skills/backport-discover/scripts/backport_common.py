"""
Shared helpers for the backport-discover.py (Stage 1: discovery + milestone
resolution) and backport-selector.py (Stage 2: backportable-set selection)
scripts.
"""

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = "apache/solr"
REPO_OWNER, REPO_NAME = REPO.split("/")
TERMINAL_STATUSES = {"skip", "backported", "ported-different-subject"}
PRUNE_AFTER_DAYS = 120
GH_GRAPHQL_BATCH_SIZE = 50

# The git remote name for the upstream Solr repo. Branch identifiers are bare
# (e.g. "branch_10x") everywhere outside this module -- CLI args, state-file
# keys, JSON output -- since this tooling only ever targets upstream and the
# remote name is a fixed local convention, not something worth threading
# through every call site. qualify()/debase() are the only place that
# convention is applied, at the boundary to actual git plumbing (fetch, log,
# merge-base) which needs a real ref.
UPSTREAM_REMOTE = "apache"


def qualify(branch):
    """Bare branch name -> fully-qualified upstream ref, for git plumbing
    (fetch/log/merge-base) that needs a real ref to resolve. Already-qualified
    input passes through unchanged (defensive)."""
    return branch if "/" in branch else f"{UPSTREAM_REMOTE}/{branch}"


def debase(branch):
    """Strip a leading '<UPSTREAM_REMOTE>/' so a possibly-qualified branch
    name normalizes to the bare form used as state-file keys / CLI values.
    Anything else (a bare name already, or a different remote) passes through
    unchanged."""
    prefix = f"{UPSTREAM_REMOTE}/"
    return branch[len(prefix):] if branch.startswith(prefix) else branch


def git(*args):
    result = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    return result.stdout


def fetch_remotes(refs):
    remotes = {ref.split("/", 1)[0] for ref in refs if "/" in ref}
    for remote in sorted(remotes):
        git("fetch", remote)


def is_ancestor(older, newer):
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer], capture_output=True
    )
    return result.returncode == 0


def branch_subjects(branch):
    # Split on a literal "\n" (matching git's own line termination), not
    # str.splitlines() -- splitlines() also breaks on Unicode line/paragraph
    # separators (U+2028/U+2029/etc.) that can appear embedded *within* a
    # single commit's %s-extracted subject, which would otherwise fragment
    # one subject into several and break exact-match lookups against it.
    return set(line for line in git("log", "--format=%s", branch).split("\n") if line)


def source_commits(source, since):
    args = ["log", "--reverse", f"--since={since}", "--format=%H\t%an\t%aI\t%cn\t%ce\t%s", source]
    out = git(*args)
    commits = []
    # Split on a literal "\n", not str.splitlines() -- see branch_subjects()
    # for why: a subject containing an embedded Unicode line separator would
    # otherwise get truncated mid-record instead of parsed as one commit.
    for line in out.split("\n"):
        if not line:
            continue
        parts = line.split("\t", 5)
        if len(parts) < 6:
            continue
        sha, commit_author, date, committer_name, committer_email, subject = parts
        commits.append({
            "sha": sha,
            "author": commit_author,
            "date": date,
            "committer_name": committer_name,
            "committer_email": committer_email,
            "subject": subject,
        })
    return commits


def parse_branch_numbers(target_branches):
    """Map release branch name -> its numeric line, e.g. branch_10x -> 10."""
    numbered = {}
    for b in target_branches:
        numbered[parse_branch_number(b)] = b
    return numbered


def parse_branch_number(branch):
    """Numeric line for a single release branch, e.g. branch_10x -> 10."""
    m = re.match(r"^(?:.*/)?branch_(\d+)x$", branch)
    if not m:
        print(f"Cannot parse branch number from '{branch}' (expected branch_Nx, optionally remote-prefixed)", file=sys.stderr)
        sys.exit(1)
    return int(m.group(1))


def milestone_number(milestone_title):
    """Parse a milestone title like '10.x' -> 10. Returns None if absent/malformed."""
    if not milestone_title:
        return None
    m = re.match(r"^(\d+)\.x$", milestone_title.strip())
    return int(m.group(1)) if m else None


def gh_graphql_fetch(shas):
    """Fetch PR number/milestone/merged_by/labels/committer_login for many commits
    in one round trip per batch, via GraphQL aliases (Commit.associatedPullRequests,
    Commit.committer) instead of per-commit REST calls.

    committer_login is the GitHub account GitHub itself resolved the commit's raw
    git committer email to (Commit.committer.user.login), independent of whether
    there's a PR -- normalizes "sometimes an email, sometimes a login" down to a
    login whenever GitHub can make that link, with merged_by/raw email as fallbacks
    (see committer_matches)."""
    results = {}
    for i in range(0, len(shas), GH_GRAPHQL_BATCH_SIZE):
        chunk = shas[i : i + GH_GRAPHQL_BATCH_SIZE]
        aliases = "\n".join(
            f'''c{idx}: object(oid: "{sha}") {{
  ... on Commit {{
    committer {{ user {{ login }} }}
    associatedPullRequests(first: 1) {{
      nodes {{
        number
        milestone {{ title }}
        mergedBy {{ login }}
        labels(first: 20) {{ nodes {{ name }} }}
      }}
    }}
  }}
}}'''
            for idx, sha in enumerate(chunk)
        )
        query = f'query {{\n  repository(owner: "{REPO_OWNER}", name: "{REPO_NAME}") {{\n{aliases}\n  }}\n}}'
        result = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"], capture_output=True, text=True
        )
        if result.returncode != 0:
            print(f"Warning: GraphQL batch fetch failed:\n{result.stderr.strip()}", file=sys.stderr)
            continue
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        repo_data = data.get("data", {}).get("repository") or {}
        for idx, sha in enumerate(chunk):
            obj = repo_data.get(f"c{idx}")
            if obj is None:
                continue
            nodes = obj.get("associatedPullRequests", {}).get("nodes", [])
            pr = nodes[0] if nodes else None
            milestone = pr.get("milestone") if pr else None
            merged_by_user = pr.get("mergedBy") if pr else None
            committer_user = (obj.get("committer") or {}).get("user")
            results[sha] = {
                "pr_number": pr.get("number") if pr else None,
                "milestone": milestone.get("title") if milestone else None,
                "labels": [l.get("name") for l in pr.get("labels", {}).get("nodes", [])] if pr else [],
                "merged_by": merged_by_user.get("login") if merged_by_user else None,
                "committer_login": committer_user.get("login") if committer_user else None,
            }
    return results


def recently_updated_milestone_prs(limit=15):
    """Cheap staleness probe: for each known release milestone (9.x/10.x/11.x),
    fetch the `limit` most-recently-updated PRs carrying that milestone right now.
    Returns {pr_number: milestone_title}.

    Comparing this against a commit's cached gh.milestone lets discovery catch a
    PR whose milestone changed *after* it was last fetched (in either direction --
    moved onto a line, or off of one) without re-fetching every already-resolved
    commit's metadata on every run. Only catches drift among recently-touched PRs;
    a milestone edit on a PR nobody has touched since falls outside the window and
    still needs --refresh to catch."""
    recent = {}
    for title in ("9.x", "10.x", "11.x"):
        result = subprocess.run(
            [
                "gh", "pr", "list", "--repo", REPO, "--state", "all",
                "--search", f"milestone:{title} sort:updated-desc",
                "--limit", str(limit), "--json", "number,milestone",
            ],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"Warning: milestone-drift probe failed for {title}:\n{result.stderr.strip()}", file=sys.stderr)
            continue
        try:
            prs = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        for pr in prs:
            milestone = pr.get("milestone") or {}
            recent[pr["number"]] = milestone.get("title")
    return recent


def committer_matches(gh_info, entity, pattern):
    """entity is a commit dict or a state entry, either of which carries
    committer_name/committer_email. A commit "counts" as committed by pattern if
    the GitHub-resolved committer login matches, or (when there's an associated
    PR) the GitHub merger login matches, or the raw git committer identity
    matches -- in that precedence order."""
    if not pattern:
        return True
    committer_login = gh_info.get("committer_login")
    if committer_login and re.search(pattern, committer_login, re.IGNORECASE):
        return True
    merged_by = gh_info.get("merged_by")
    if merged_by and re.search(pattern, merged_by, re.IGNORECASE):
        return True
    committer = f'{entity.get("committer_name", "")} <{entity.get("committer_email", "")}>'
    return re.search(pattern, committer, re.IGNORECASE) is not None


def self_committer_pattern():
    """Regex matching the current user's identity, used to scope ambiguous-
    milestone commits to them by default. Prefers the GitHub login (resolved via
    `gh api user`) since committer_matches checks committer_login/merged_by --
    both GitHub logins -- before falling back to the raw git identity; falls back
    to git user.email if gh isn't authenticated."""
    login_result = subprocess.run(
        ["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True
    )
    login = login_result.stdout.strip()
    if login_result.returncode == 0 and login:
        return re.escape(login)
    email = subprocess.run(
        ["git", "config", "user.email"], capture_output=True, text=True
    ).stdout.strip()
    return re.escape(email) if email else None


def load_state(path):
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        print(
            f"State file not found: {path}\n"
            f"Initialize it first:  echo '{{}}' > {path}",
            file=sys.stderr,
        )
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"State file is not valid JSON: {path}\n{e}", file=sys.stderr)
        sys.exit(1)
    data.setdefault("subjects", {})
    return data


def save_state(path, state):
    with open(path, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)
        f.write("\n")


def prune_state(state, target_branches):
    cutoff = datetime.now(timezone.utc) - timedelta(days=PRUNE_AFTER_DAYS)
    to_delete = []
    for subject, entry in state["subjects"].items():
        if entry.get("milestone_override"):
            # Manually-recorded decision for a PR-less commit -- unlike a real
            # GitHub PR milestone, this only exists here; pruning it would lose
            # it permanently with no way to re-derive it later.
            continue
        targets = entry.get("targets", {})
        relevant = {b: targets.get(b, {}).get("status") for b in target_branches if b in targets}
        if not relevant or not all(status in TERMINAL_STATUSES for status in relevant.values()):
            continue
        updated_at = entry.get("updated_at")
        if not updated_at:
            continue
        try:
            updated_dt = datetime.fromisoformat(updated_at)
        except ValueError:
            continue
        if updated_dt < cutoff:
            to_delete.append(subject)
    for subject in to_delete:
        del state["subjects"][subject]


def ci_high_water_mark(source, skip):
    if skip:
        return None, []
    script = Path(__file__).resolve().parent / "ci-status.py"
    result = subprocess.run(
        ["python3", str(script), "--branch", qualify(source)], capture_output=True, text=True
    )
    if result.returncode != 0:
        print(f"Warning: ci-status.py failed, not filtering by CI coverage:\n{result.stderr.strip()}", file=sys.stderr)
        return None, []
    data = json.loads(result.stdout)
    return data.get("high_water_mark"), data.get("jobs", [])
