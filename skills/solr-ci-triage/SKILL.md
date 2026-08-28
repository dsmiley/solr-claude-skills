---
name: solr-ci-triage
description: Investigate Solr CI test failures on Jenkins (ci-builds.apache.org) and check per-test flaky history via the Develocity API (develocity.apache.org). Use when the user asks why a Jenkins build (e.g. Solr-Test-main) is failing/UNSTABLE, which tests failed in a build, whether a CI failure is a flake or a regression, or wants a test's recent pass/fail/flaky stats from Develocity. For long-term (pre-28-day) failure-rate history, the sister skill solr-fucit-flaky-tests covers fucit.org.
---

# Solr CI triage

Two data sources, one script: `scripts/ci-triage.py` (stdlib-only python3, prints JSON).

```bash
# Failed tests in a Jenkins build (+ changeset, node, per-test age & error)
python3 ${CLAUDE_PLUGIN_ROOT}/skills/solr-ci-triage/scripts/ci-triage.py failures --job Solr-Test-main --build latest

# Develocity outcome history for a test (class, FQCN, or with .method suffix)
source ~/.zshrc  # DEVELOCITY_ACCESS_KEY lives in .zshrc; non-interactive shells don't source it
python3 ${CLAUDE_PLUGIN_ROOT}/skills/solr-ci-triage/scripts/ci-triage.py history TestDistributedGrouping --days 28 --scans

# SOLR JIRA issues mentioning the test class (anonymous; accepts FQCN/.method too)
python3 ${CLAUDE_PLUGIN_ROOT}/skills/solr-ci-triage/scripts/ci-triage.py jira TestDistributedGrouping
```

Typical triage flow: `failures` on the unstable build -> for each failed test, `history` to
classify flake vs regression -> **always also run `jira`** on the test class: known-flaky
tests often have an open issue, and a recently-resolved one can implicate a fresh fix as the
regression source. Report relevant hits (open flaky-test issues, recent fixes touching the
test) alongside the flake/regression verdict. Cross-check long-term trends via the
solr-fucit-flaky-tests skill (fucit.org archives per-test rates back to 2017; Develocity data
here is bounded by scan retention).

## Jenkins (no auth)

Base: `https://ci-builds.apache.org/job/Solr/job/<job>/` -- jobs of interest: `Solr-Test-main`,
`Solr-Lint-main`, `Solr-TestIntegration-main`. JSON API endpoints the script uses:

- `<n>/api/json?tree=result,builtOn,timestamp,changeSets[items[commitId,msg,author[fullName]]]`
- `<n>/testReport/api/json?tree=failCount,...,suites[cases[className,name,status,age,duration,errorDetails]]`
  -- full report is ~300KB for 8k+ tests; always use `tree=` filters. `age` = consecutive
  failing builds. Failed statuses are `FAILED`/`REGRESSION`.
- Per-test page across builds: `<n>/testReport/<pkg>/<Class>/<method>/api/json?tree=status,age`
  -- **404 on some builds is normal** (report purged or build died early); tolerate it.
- `UNSTABLE` = test failures; `FAILURE` = build/infra broke before or during tests.

## Develocity API (needs access key)

- Live host is **develocity.apache.org** (ge.apache.org is dead). The web UI is a JS app --
  WebFetch/curl get an empty shell; use the REST API.
- Auth: `DEVELOCITY_ACCESS_KEY` env var, format `develocity.apache.org=<key>` (same var the
  Develocity Gradle plugin reads). Send as `Authorization: Bearer <key-part>`. Keys are
  generated at develocity.apache.org -> My settings -> Access keys; ASF granted API access
  ("Access build data via the API") to **all @apache.org accounts** per INFRA-27815.
- **401** = key invalid (a truncated copy is the classic cause -- keys are ~52 chars);
  **403** = permission missing. The token-exchange endpoint `POST /api/auth/token` also
  distinguishes: it 401s on a bad key.
- `GET /api/tests/cases` gotchas (all discovered the hard way):
  - `query` **must** contain a `buildStartTime` term, e.g.
    `project:solr-root and buildStartTime>-28d`.
  - The Develocity project name is **`solr-root`**, not `solr`.
  - `testOutcomes` is a **repeated** param (`testOutcomes=failed&testOutcomes=flaky`), not
    comma-joined. Enum: passed, failed, skipped, flaky, notSelected.
  - `container` must be the **exact FQCN** -- wildcards like `*TestFoo` silently return
    nothing. The script resolves bare class names via `git ls-files` in the checkout.
  - `include=buildScanIds` adds `buildScanIdsByOutcome`; scan URL is
    `https://develocity.apache.org/s/<id>`.
- "flaky" in Develocity = failed then passed on retry within the same build; a chronically
  flaky test shows `flaky > 0, failed == 0` week after week.
- `GET /api/builds?query=...&maxBuilds=N&models=gradle-attributes` for build-level queries
  (root project names, agents, etc.).

## Per-branch history comparison

Solr build scans carry no git-branch value, but `gradle/develocity.gradle` tags each CI scan
with the Jenkins job name minus its `Solr-`/`Solr/` parts. Per-branch test jobs exist
(`Solr-Test-main`, `Solr-Test-9.x`, `Solr-Test-10.x`, plus versioned ones like
`Solr-Test-9.11`), so branch = tag:

```bash
# same query per branch; ~186 runs/28d per branch job as of 2026
... 'query=project:solr-root and buildStartTime>-28d and tag:Test-9.x'   # branch_9x
... 'query=... and tag:Test-10.x'                                        # branch_10x
... 'query=... and tag:Test-main'                                        # main
```

Identical failed-counts across *unrelated* suites within one tag usually means a few bad
builds (environmental), not per-test flakiness.

- `GET /api/tests/containers` (same params as `/api/tests/cases`) aggregates at class level;
  prefer it when hunting suite-level (`classMethod` / `@BeforeClass` / `@AfterClass`) failures,
  which show every *method* as passed and so are invisible in method-level outcomes.
- **Crave and other unauthenticated builds never publish scans**
  (`publishing.onlyIf { it.authenticated }`), so crave test runs are invisible here; fetch
  their logs via `crave getlog --jobID <N>` instead.
