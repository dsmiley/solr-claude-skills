#!/usr/bin/env python3
"""Solr CI triage: Jenkins build failures + Develocity per-test history.

Subcommands:
  failures [--job JOB] [--build N|latest]   Failed tests in a Jenkins build, with changeset.
  history TEST [--days N] [--scans]         Develocity outcome distribution for a test class/method.

Prints JSON to stdout. Requires no auth for `failures`; `history` needs
DEVELOCITY_ACCESS_KEY (format "develocity.apache.org=<key>", as the Develocity
Gradle plugin uses). Non-interactive shells don't source ~/.zshrc, so callers
may need `source ~/.zshrc` first.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

JENKINS_BASE = "https://ci-builds.apache.org/job/Solr/job"
DEVELOCITY_HOST = "develocity.apache.org"
FAILED_STATUSES = {"FAILED", "REGRESSION"}


def fetch_json(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def die(msg):
    print(json.dumps({"error": msg}))
    sys.exit(1)


# ---------- failures ----------


def cmd_failures(args):
    job = args.job
    build = args.build
    if build == "latest":
        meta = fetch_json(f"{JENKINS_BASE}/{job}/lastCompletedBuild/api/json?tree=number")
        build = meta["number"]

    # freestyle jobs expose changeSet (singular); pipeline jobs changeSets
    info = fetch_json(
        f"{JENKINS_BASE}/{job}/{build}/api/json"
        "?tree=result,builtOn,timestamp,changeSet[items[commitId,msg,author[fullName]]],"
        "changeSets[items[commitId,msg,author[fullName]]]"
    )
    changesets = info.get("changeSets") or []
    if info.get("changeSet"):
        changesets = changesets + [info["changeSet"]]
    out = {
        "job": job,
        "build": build,
        "url": f"{JENKINS_BASE}/{job}/{build}/",
        "result": info.get("result"),
        "builtOn": info.get("builtOn"),
        "changeset": [
            {
                "sha": i["commitId"][:11],
                "author": (i.get("author") or {}).get("fullName"),
                "msg": i["msg"],
            }
            for cs in changesets
            for i in cs.get("items", [])
        ],
    }

    try:
        report = fetch_json(
            f"{JENKINS_BASE}/{job}/{build}/testReport/api/json"
            "?tree=failCount,passCount,skipCount,"
            "suites[cases[className,name,status,age,duration,errorDetails]]"
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            out["testReport"] = None  # no report: purged, or build died before tests
            print(json.dumps(out, indent=2))
            return
        raise

    out["counts"] = {
        "failed": report.get("failCount"),
        "passed": report.get("passCount"),
        "skipped": report.get("skipCount"),
    }
    out["failures"] = [
        {
            "className": c["className"],
            "name": c["name"],
            "age": c.get("age"),  # consecutive failing builds
            "duration": c.get("duration"),
            "errorDetails": (c.get("errorDetails") or "")[:600],
        }
        for s in report.get("suites", [])
        for c in s.get("cases", [])
        if c.get("status") in FAILED_STATUSES
    ]
    print(json.dumps(out, indent=2))


# ---------- jira ----------


def cmd_jira(args):
    # last capitalized segment = class name (strips both package and .method suffix)
    term = next((s for s in reversed(args.term.split(".")) if s[:1].isupper()), args.term)
    params = urllib.parse.urlencode(
        {
            "jql": f'project = SOLR AND text ~ "{term}" ORDER BY updated DESC',
            "fields": "key,summary,status,resolution,updated",
            "maxResults": args.max,
        }
    )
    data = fetch_json(f"https://issues.apache.org/jira/rest/api/2/search?{params}")
    out = {
        "term": term,
        "total": data.get("total"),
        "issues": [
            {
                "key": i["key"],
                "url": f"https://issues.apache.org/jira/browse/{i['key']}",
                "status": i["fields"]["status"]["name"],
                "resolution": (i["fields"].get("resolution") or {}).get("name"),
                "updated": i["fields"]["updated"][:10],
                "summary": i["fields"]["summary"],
            }
            for i in data.get("issues", [])
        ],
    }
    print(json.dumps(out, indent=2))


# ---------- history ----------


def develocity_key():
    raw = os.environ.get("DEVELOCITY_ACCESS_KEY", "")
    if not raw:
        die(
            "DEVELOCITY_ACCESS_KEY not set. It lives in ~/.zshrc; "
            "non-interactive shells must `source ~/.zshrc` first."
        )
    for part in raw.split(";"):
        if "=" in part:
            host, key = part.split("=", 1)
            if host.strip() == DEVELOCITY_HOST:
                return key.strip()
    if "=" not in raw:
        return raw.strip()  # bare key
    die(f"No key for {DEVELOCITY_HOST} in DEVELOCITY_ACCESS_KEY")


def resolve_fqcn(name):
    """Resolve a bare class name to its FQCN via the git checkout (cwd)."""
    try:
        paths = subprocess.run(
            ["git", "ls-files", f"*/{name}.java"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        paths = []
    if not paths:
        die(f"Cannot resolve '{name}' to a fully-qualified class name; pass the FQCN, or run from the Solr checkout")
    path = min(paths, key=lambda p: ("/test/" not in p, len(p)))  # prefer test sources
    with open(path, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"\s*package\s+([\w.]+)\s*;", line)
            if m:
                return f"{m.group(1)}.{name}"
    die(f"No package declaration found in {path}")


def split_test_arg(arg):
    """'Cls' | 'a.b.Cls' | 'Cls.method' | 'a.b.Cls.method' -> (container-ish, method|None)."""
    parts = arg.split(".")
    method = None
    if len(parts) >= 2 and parts[-1][:1].islower() and parts[-2][:1].isupper():
        method = parts.pop()
    cls = ".".join(parts)
    if "." not in cls:
        cls = resolve_fqcn(cls)
    return cls, method


def cmd_history(args):
    key = develocity_key()
    container, method = split_test_arg(args.test)
    params = [
        ("query", f"project:solr-root and buildStartTime>-{args.days}d"),
        ("container", container),
        ("testOutcomes", "passed"),
        ("testOutcomes", "failed"),
        ("testOutcomes", "flaky"),
    ]
    if args.scans:
        params.append(("include", "buildScanIds"))
    url = f"https://{DEVELOCITY_HOST}/api/tests/cases?" + urllib.parse.urlencode(params)
    try:
        data = fetch_json(url, headers={"Authorization": f"Bearer {key}"})
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:400]
        hint = " (401: bad/truncated key; 403: missing API permission)" if e.code in (401, 403) else ""
        die(f"Develocity API HTTP {e.code}{hint}: {body}")

    cases = data.get("content", [])
    if method:
        cases = [c for c in cases if c["name"].endswith(f".{method}")]
    out = {"container": container, "days": args.days, "cases": []}
    for c in cases:
        entry = {"name": c["name"], "outcomes": c["outcomeDistribution"]}
        ids = c.get("buildScanIdsByOutcome") or {}
        scan_urls = [
            f"https://{DEVELOCITY_HOST}/s/{sid}"
            for outcome in ("failed", "flaky")
            for sid in ids.get(outcome, [])
        ]
        if scan_urls:
            entry["problemScans"] = scan_urls
        out["cases"].append(entry)
    print(json.dumps(out, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("failures", help="failed tests in a Jenkins build")
    f.add_argument("--job", default="Solr-Test-main")
    f.add_argument("--build", default="latest")
    f.set_defaults(func=cmd_failures)

    h = sub.add_parser("history", help="Develocity outcome history for a test")
    h.add_argument("test", help="Class, FQCN, or either with .method suffix")
    h.add_argument("--days", type=int, default=28)
    h.add_argument("--scans", action="store_true", help="include failed/flaky build-scan URLs")
    h.set_defaults(func=cmd_history)

    j = sub.add_parser("jira", help="search SOLR JIRA issues mentioning a test class")
    j.add_argument("term", help="Class name or FQCN (package is stripped)")
    j.add_argument("--max", type=int, default=10)
    j.set_defaults(func=cmd_jira)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
