---
name: solr-fucit-flaky-tests
description: Check the CI failure/flakiness status of Solr tests using fucit.org's solr-jenkins-reports aggregator (hossman's Jenkins report site). Use whenever the user asks whether a test is flaky, how often a test fails in CI/Jenkins, what the flakiest tests are lately, whether a test recently started failing (regression), the failure-rate trend of a test over time (per-test history archived back to 2017 — far longer than Develocity), or mentions fucit.org, "jenkins reports", or "BadApples". Works even if the user only names a test class and asks "does this fail in CI?".
---

# Solr flaky-test status via fucit.org

Base URL: `http://fucit.org/solr-jenkins-reports/`

The site aggregates all Lucene/Solr Jenkins servers (Apache ci-builds, Policeman/thetaphi, etc.), archives data from failing builds for **7 days**, and publishes failure-rate reports. Data files regenerate roughly daily; the site structure is stable.

Its two primary values:
1. **Identifying flaky/failing tests**: which tests fail, which are most flaky, and which *recently became* flaky.
2. **Deep history**: per-test failure data archived back to **Nov 2017** (daily + weekly snapshots) — far longer retention than Develocity.

## Access rules

- Use **plain `http://` with curl**. The HTTPS certificate is broken (WebFetch fails with a cert error).
- The `.html` report pages render client-side with JavaScript (Tabulator) — **useless via curl**. All the data behind them is in `reports/` as CSV/JSON. Always go straight to the raw files.
- Files are small (5–100 KB). Download to the scratchpad, then grep/jq.

## The raw data files (`reports/`)

Directory listing: `http://fucit.org/solr-jenkins-reports/reports/`

| File | What it is |
|---|---|
| `7days-failure-rates.csv` / `.json` | Failure rate per test method and per suite, past 7 days (rolling). The main "how flaky is X" report. |
| `24hours-failure-rates.csv` / `.json` | Same, past 24 hours — "is it failing right now". |
| `suspicious-failure-rates.json` | Methods whose past-7-days rate **increased** vs the previous 3 calendar weeks — the "newly flaky / regression" report. |
| `failure-rate-history.csv` | Weekly failure-rate time series (rows back to 2018) — long-term trend. Columns = only the methods that failed in the past 7 days, so the column set shifts as data changes. |
| `7days-top-failing-classes.csv` | `count,class` — quick leaderboard of worst classes. |
| `7days-method-failures.csv` | One row per individual method failure: `class,method(),jobPath/`. |
| `{24hours,7days}-test-results.txt` | List of per-build `jenkins.tests.csv` paths that fed the report. |
| `archive/weekly/YYYY-WW.failure-rates.csv.gz` | Snapshot of the failure-rates CSV for each ISO week, back to `2017-47`. Same columns as the current CSV. The real long-term history. |
| `archive/daily/YYYY-MM-DD.failure-rates.csv.gz` | Same, per day, back to 2017-11-21. |
| `archive/{weekly,daily}/*.method-failures.csv.gz` | Archived per-failure listings (same format as `7days-method-failures.csv`). |

### Formats

`*-failure-rates.csv` columns (no header): `class,method,rate,failures,runs,,jobPath1,jobPath2,...`
- `method` empty = suite-level row (suite init/teardown failures; counts unreliable per the site's own caveats).
- `rate` = failures/runs as a **fraction** — can exceed 1.0 for suites (multiple suite-level failures per run).

`*-failure-rates.json` / `suspicious-failure-rates.json`: array of objects with `class`, `method`, `fail_rate` (**percent**, string), `failures`, `runs`, `failed_jobs: [{path, count}]`. Suspicious adds `historic_fail_rate`, `historic_runs`, and `delta_fail_rate` (sort key for regressions). Failure-rates JSON also has `"suite":true` on suite rows.

`failure-rate-history.csv`: header row `Date,<fqcn.method>,...`; values are fractions per week.

## Recipes

Set `R=http://fucit.org/solr-jenkins-reports/reports` for brevity.

**Is test X flaky? (7-day status)**
```bash
curl -s $R/7days-failure-rates.csv | grep -i MyTestName
```
Columns 3–5 give rate, failures, runs. Not listed = zero failures in 7 days (only failing tests appear in any report).

**Flakiest tests right now**
```bash
curl -s $R/7days-failure-rates.csv | awk -F, '$2!=""' | head -20   # methods, pre-sorted by rate desc
curl -s $R/7days-top-failing-classes.csv | head                    # per-class failure counts
```

**Newly flaky / did something regress?**
```bash
curl -s $R/suspicious-failure-rates.json | jq -r '.[] | [.delta_fail_rate, .class, .method, "\(.failures)/\(.runs)"] | @tsv' | sort -rn | head -20
```

**Long-term trend for one test** (e.g. did my fix from N weeks ago help?)

Quick path — `failure-rate-history.csv` has weekly rates back to 2018, but only columns for tests failing in the *current* 7-day window:
```bash
curl -s $R/failure-rate-history.csv > hist.csv
col=$(head -1 hist.csv | tr ',' '\n' | grep -n MyTestName | cut -d: -f1)
cut -d, -f1,$col hist.csv | tail -15
```

Full path — works for **any** test over **any** period since Nov 2017, via the weekly archives (ISO week numbers):
```bash
for wk in 2026-{20..31}; do
  printf '%s ' $wk
  curl -s $R/archive/weekly/$wk.failure-rates.csv.gz | gunzip | grep MyTestName || echo "-"
done
```
Each matching line has the same `class,method,rate,failures,runs,,jobs...` columns. Use `archive/daily/YYYY-MM-DD.*` for day-level resolution. Fetch sequentially and be modest about how many files you pull (small hobby server).

**Which builds did it fail in?** The `failed_jobs[].path` values are `<server>/<jobName>/<buildNumber>` (e.g. `apache_solr/Solr-Test-main/14412`), so the job and build are readable from the path itself. Each path indexes into the archived build data:
```
http://fucit.org/solr-jenkins-reports/job-data/<path>/
```
Each build dir has `jenkins.tests.csv` (`STATUS,class,method,detail` for every test in the build), `jenkins.log.txt.gz` (full console log — `curl -s ... | zgrep -A20 MyTestName` for stack traces), `jenkins.summary.xml`, and `url.txt` — the exact original Jenkins build URL (verified: `apache_solr/...` → `ci-builds.apache.org`, `thetaphi_*/...` → `jenkins.thetaphi.de`). Fetch `url.txt` when you need to cross-reference back to Jenkins. Archived only for builds with failures, only for 7 days.

**Per-branch breakdown** — the branch is encoded in each job path's job name (`Solr-Test-9.x`, `Solr-Test-10.x`, `Solr-Test-main`, `Solr-NightlyTests-main`, thetaphi's `Solr-main-Linux`, …), so a test's failures can be split by branch by counting the trailing job columns of a failure-rates CSV:
```bash
curl -s $R/7days-failure-rates.csv | python3 -c '
import csv, re, sys, collections
for row in csv.reader(sys.stdin):
    if len(row) < 7 or "MyTestName" not in row[0]: continue
    c = collections.Counter()
    for j in row[6:]:
        if not j: continue
        m = re.search(r"-(main|\d+\.?x|\d+\.\d+)[/-]", j)  # 9.x, 10x, 9.11, main
        c[m.group(1) if m else "?"] += 1
    print(f"{row[0]}.{row[1]}: {row[3]}/{row[4]}  by-branch: {dict(c)}")'
```
Compare against per-branch *run counts* (fucit has none — use Develocity per-branch tags via the solr-ci-triage skill) before concluding one branch is flakier: main's jobs run far more often than release-branch jobs.

## Caveats

- All reports cover **failures only**; absence means "not failing", not "not running". Nightly-only tests have few runs, so rates there are noisy.
- Suite-level rows (empty method) have unreliable run counts — prefer method rows.
- Data includes both Lucene and Solr jobs across many branches/JVMs; a "failure" may be branch-specific — check the `failed_jobs` paths before concluding a test is broken on main.
