# solr-dev

Claude Code skills for [Apache Solr](https://solr.apache.org/) development, packaged as a
plugin. Backporting commits to release branches, CI failure triage, and branch commit
messages.

Not affiliated with or endorsed by the Apache Software Foundation.

## Skills

| Skill | What it does |
|-------|--------------|
| `backport` | Cherry-pick commits from `main` to a release branch with LLM-assisted conflict resolution and a two-phase test runner (local compile+lint, then crave for tests). Give it commit hashes, or no args to triage-and-execute. |
| `backport-discover` | Report-only backport triage: discover pending commits, refresh GitHub PR milestone metadata, and select what's safely backportable right now (CI-gated). No execution. |
| `solr-ci-triage` | Investigate Jenkins build failures on ci-builds.apache.org and check per-test flaky history via the Develocity API. |
| `solr-fucit-flaky-tests` | Check a test's CI failure/flakiness trend using fucit.org's Jenkins report aggregator (history back to 2017). |
| `branch-commit-msg` | Summarize the current branch's commits vs. `main` into one commit message, copied to the clipboard. |

## Install

```
/plugin marketplace add dsmiley/solr-claude-skills
/plugin install solr-dev@solr-claude-skills
```

## Requirements

- `python3` (standard library only; no pip installs)
- [`gh`](https://cli.github.com/) CLI, authenticated — used by `backport`, `backport-discover`
- `DEVELOCITY_ACCESS_KEY` in the environment for `solr-ci-triage` Develocity queries,
  formatted `develocity.apache.org=<key>`. Non-interactive shells don't source `~/.zshrc`,
  so the skill may `source ~/.zshrc` first.

## Backport state file

`backport` and `backport-discover` track milestone resolutions and completion in a
machine-local file at `~/.claude/solr-backport-state.json`, outside this repo. Initialize
it once:

```
echo '{}' > ~/.claude/solr-backport-state.json
```

## License

[Apache-2.0](LICENSE).
