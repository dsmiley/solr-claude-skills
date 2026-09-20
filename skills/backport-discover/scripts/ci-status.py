#!/usr/bin/env python3
"""
Report the CI "high-water-mark" for a Solr branch: the newest commit that has
been verified green across all of Lint, Test, TestIntegration (and Smoketest,
on release branches), plus the Docker Nightly GitHub Actions workflow.

For each job, the "frontier" is the sha of its most recent SUCCESS build,
walking back past any later failures/pending builds (a green build still
covers earlier commits even if a later build on a newer commit failed). The
high-water-mark is the oldest of the per-job frontiers, since a commit is only
fully verified once every job has passed at or after it.

Output (stdout): JSON with per-job frontier/latest-build info and the overall
high_water_mark sha (null if it couldn't be determined for any job). Jobs named
in --ignore-jobs are reported (ignored: true) but don't hold the mark back.

Remotes referenced by --branch (e.g. the "apache" in "apache/main") are
fetched automatically.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

REPO = "apache/solr"
JENKINS_BASE = "https://ci-builds.apache.org/job/Solr/job"
JOB_BASES = ["Solr-Lint", "Solr-Test", "Solr-TestIntegration"]
SMOKETEST_BASE = "Solr-Smoketest"


def git(*args):
    result = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        print(result.stderr.strip(), file=sys.stderr)
        sys.exit(1)
    return result.stdout


def is_ancestor(older, newer):
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", older, newer], capture_output=True
    )
    return result.returncode == 0


def fetch_remote_if_needed(ref):
    if "/" in ref:
        git("fetch", ref.split("/", 1)[0])


def gh_api(path):
    result = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def jenkins_suffix(bare_branch):
    if bare_branch == "main":
        return "main"
    m = re.match(r"^branch_(\d+)x$", bare_branch)
    if not m:
        print(f"Cannot derive Jenkins suffix from branch '{bare_branch}' (expected main or branch_Nx)", file=sys.stderr)
        sys.exit(1)
    return f"{m.group(1)}.x"


def fetch_jenkins_builds(job_name):
    url = (
        f"{JENKINS_BASE}/{job_name}/api/json"
        "?tree=builds%5Bnumber%2Ctimestamp%2Cresult%2Curl%2Cactions%5BlastBuiltRevision%5BSHA1%5D%5D%5D%7B,20%7D"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        return None, str(e)
    builds = []
    for b in data.get("builds", []):
        sha = None
        for a in b.get("actions", []):
            if "lastBuiltRevision" in a:
                sha = a["lastBuiltRevision"]["SHA1"]
        builds.append({
            "number": b.get("number"),
            "result": b.get("result"),
            "sha": sha,
            "url": b.get("url"),
            "timestamp": b.get("timestamp"),
        })
    builds.sort(key=lambda b: b["number"] or 0, reverse=True)
    return builds, None


def jenkins_job_status(job_name):
    builds, error = fetch_jenkins_builds(job_name)
    if error:
        return {"name": job_name, "error": error}
    if not builds:
        return {"name": job_name, "error": "no builds returned"}

    latest = builds[0]
    latest_ts = (
        datetime.fromtimestamp(latest["timestamp"] / 1000, tz=timezone.utc).isoformat()
        if latest.get("timestamp") else None
    )

    frontier = None
    for b in builds:
        if b["result"] == "SUCCESS" and b["sha"]:
            frontier = b
            break

    return {
        "name": job_name,
        "latest_sha": latest["sha"],
        "latest_status": latest["result"],
        "latest_build_url": latest["url"],
        "latest_timestamp": latest_ts,
        "frontier_sha": frontier["sha"] if frontier else None,
        "frontier_build_url": frontier["url"] if frontier else None,
        "frontier_timestamp": (
            datetime.fromtimestamp(frontier["timestamp"] / 1000, tz=timezone.utc).isoformat()
            if frontier and frontier.get("timestamp") else None
        ),
        "error": None if frontier else "no SUCCESS build in recent window",
    }


def docker_nightly_status(bare_branch):
    data = gh_api(f"repos/{REPO}/actions/workflows/docker-nightly.yml/runs?branch={bare_branch}&per_page=20")
    if data is None:
        return {"name": "docker-nightly", "error": "gh api call failed"}
    runs = data.get("workflow_runs", [])
    if not runs:
        return {"name": "docker-nightly", "error": "no runs returned"}

    latest = runs[0]

    frontier = None
    for r in runs:
        if r.get("status") == "completed" and r.get("conclusion") == "success" and r.get("head_sha"):
            frontier = r
            break

    return {
        "name": "docker-nightly",
        "latest_sha": latest.get("head_sha"),
        "latest_status": latest.get("conclusion") or latest.get("status"),
        "latest_build_url": latest.get("html_url"),
        "latest_timestamp": latest.get("run_started_at"),
        "frontier_sha": frontier.get("head_sha") if frontier else None,
        "frontier_build_url": frontier.get("html_url") if frontier else None,
        "frontier_timestamp": frontier.get("run_started_at") if frontier else None,
        "error": None if frontier else "no successful run in recent window",
    }


def pick_older(a, b):
    if a == b:
        return a
    if is_ancestor(a, b):
        return a
    if is_ancestor(b, a):
        return b
    print(f"Warning: {a} and {b} are not on the same history; cannot compare", file=sys.stderr)
    return a


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--branch", default="apache/main")
    parser.add_argument("--check-sha", default=None, help="Also report whether this sha is covered by the high-water-mark")
    parser.add_argument(
        "--ignore-jobs", default="",
        help="Comma-separated job names (as reported in the output, e.g. docker-nightly) to exclude "
        "from the high-water-mark, e.g. while one is known-broken for unrelated reasons. "
        "Ignored jobs are still reported, marked ignored.",
    )
    args = parser.parse_args()

    fetch_remote_if_needed(args.branch)
    bare_branch = args.branch.rsplit("/", 1)[-1]
    suffix = jenkins_suffix(bare_branch)

    job_bases = list(JOB_BASES)
    if bare_branch != "main":
        job_bases.append(SMOKETEST_BASE)

    jobs = [jenkins_job_status(f"{base}-{suffix}") for base in job_bases]
    jobs.append(docker_nightly_status(bare_branch))

    ignored = {n for n in args.ignore_jobs.split(",") if n}
    for j in jobs:
        j["ignored"] = j["name"] in ignored

    frontiers = [j["frontier_sha"] for j in jobs if j.get("frontier_sha") and not j["ignored"]]
    high_water_mark = None
    if frontiers:
        high_water_mark = frontiers[0]
        for f in frontiers[1:]:
            high_water_mark = pick_older(high_water_mark, f)

    result = {"branch": args.branch, "jobs": jobs, "high_water_mark": high_water_mark}

    if args.check_sha:
        covered = bool(high_water_mark) and (
            args.check_sha == high_water_mark or is_ancestor(args.check_sha, high_water_mark)
        )
        result["check_sha"] = {"sha": args.check_sha, "covered": covered}

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
