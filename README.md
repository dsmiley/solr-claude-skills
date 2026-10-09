# solr-dev

Claude Code skills for [Apache Solr](https://solr.apache.org/) development, packaged as a
plugin. Backporting commits to release branches, CI failure triage, and branch commit
messages.

Not affiliated with or endorsed by the Apache Software Foundation.

## Skills

| Skill | What it does |
|-------|--------------|
| `backport` | Cherry-pick commits from `main` to a release branch with LLM-assisted conflict resolution and a two-phase test runner (local compile+lint, then the full test suite via a configurable command). Give it commit hashes, or no args to triage-and-execute. |
| `backport-discover` | Report-only backport triage: discover pending commits, refresh GitHub PR milestone metadata, and select what's safely backportable right now (CI-gated). No execution. |
| `solr-ci-triage` | Investigate Jenkins build failures on ci-builds.apache.org and check per-test flaky history via the Develocity API. |
| `solr-fucit-flaky-tests` | Check a test's CI failure/flakiness trend using fucit.org's Jenkins report aggregator (history back to 2017). |
| `branch-commit-msg` | Summarize the current branch's commits vs. `main` into one commit message, copied to the clipboard. |
| `write-changelog` | Generate a changelog YAML entry (via the `writeChangelog` / `writeChangelogPr` Gradle tasks) for the current branch or a given PR, customize its title and type, strip the comment block, and git-add it. |

## Install

```
/plugin marketplace add dsmiley/solr-claude-skills
/plugin install solr-dev@solr-claude-skills
```

## Requirements

- `python3` (standard library only; no pip installs)
- [`gh`](https://cli.github.com/) CLI, authenticated — used by `backport`, `backport-discover`.
  Setting PR milestones needs Solr committer rights.
- A git remote pointing at `apache/solr`, under any name; the backport skills find it by URL.
- `DEVELOCITY_ACCESS_KEY` in the environment for `solr-ci-triage` Develocity queries,
  formatted `develocity.apache.org=<key>`. Non-interactive shells don't source `~/.zshrc`,
  so the skill may `source ~/.zshrc` first.

## Backport configuration

Optional. Defaults are in [`skills/backport/config.json`](skills/backport/config.json); override
any key in your own `~/.claude/solr-backport-config.json` (don't edit the shipped file):

```json
{
  "test_all_command": "my-remote-build ./gradlew --console=plain test --continue",
  "browser_command": "open -a Firefox"
}
```

- `test_all_command` — runs the whole test suite from the repo root. Defaults to a local
  `./gradlew --console=plain test --continue`, which `backport` asks about before starting in the
  background. Point it at a remote build machine or service if you have one.
- `browser_command` — opens the JIRA URLs appended to it. Defaults to the platform opener
  (`open` / `xdg-open`).

## Backport state file

`backport` and `backport-discover` track milestone resolutions and completion in a
machine-local file at `~/.claude/solr-backport-state.json`, outside this repo. Initialize
it once:

```
echo '{}' > ~/.claude/solr-backport-state.json
```

What's already backported is re-derived from git and PR milestones from GitHub on every run, so
a fresh state file mostly just works. What lives *only* there are your decisions: milestones
for commits that have no PR, permanent skips ("doesn't belong on this branch"), deferrals, and
commits recorded as already ported under a different subject. A second machine starting from
`{}` re-asks those. To carry them along, keep the file in a synced location and symlink it into
`~/.claude/`.

The file is per-user, not shared between committers, and triage mode proposes everyone's pending
commits, not just yours. Coordinate so that one person drives backports to a given branch at a
time; otherwise two people can cherry-pick the same commits before either pushes.

## License

[Apache-2.0](LICENSE).
