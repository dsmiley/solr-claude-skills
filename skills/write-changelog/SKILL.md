---
name: write-changelog
description: Generate a Solr changelog YAML entry for the current branch or a given PR, then customize the title and type and git-add it. Use when the user wants a changelog entry for their change.
user_invocable: true
argument-hint: [pr-number-or-url]
---

# write-changelog

Generates a changelog fragment under `changelog/unreleased/`, fills in a good
`title:` and `type:`, strips the instructional comment block, and stages the file.

## Steps

1. Determine the PR number.
   - If a PR number or URL was given as the argument, use it.
   - Otherwise, try to auto-detect a PR for the current branch. Even with no
     argument there is often an open PR whose head is this branch (pushed to a
     fork remote). Try, in order, until one returns a PR:
     ```bash
     # gh's own auto-detection (uses the branch's upstream / matching head)
     gh pr view --json number,url,title,body,state

     # explicit: any open PR in apache/solr whose head ref name matches this branch
     gh pr list --repo apache/solr --state open \
       --head "$(git rev-parse --abbrev-ref HEAD)" \
       --json number,url,title,state
     ```
   - If a PR is found, note its number, title, and body for step 3.
   - If none is found, proceed without a PR.

2. Generate the file with Gradle (run from the repo root):
   - With a PR number:
     ```bash
     ./gradlew writeChangelogPr -Ppr=<number>
     ```
   - Without a PR:
     ```bash
     ./gradlew writeChangelog
     ```
   The task prints `Generated file: changelog/unreleased/<name>.yml` — that is the file to edit.

3. Gather context for the title:
   - PR case: use the `title` and `body` already fetched in step 1 (or re-fetch with
     `gh pr view <number> --json title,body`).
   - Non-PR case: `git log --oneline main...HEAD --no-merges` and read the commit messages.

4. Rewrite `title:` based on that context. Guidance from `dev-docs/changelog.adoc`:
   - Audience is end-users and administrators, not committers.
   - Short, focused on user impact. Multiple sentences is fine. No geeky internals.
   - The generated title (from branch/PR name) is usually a weak starting point — replace it.

5. Fill in `type:` with a single value, guessed from the comment block in the generated
   file (which documents each type):
   - `added` — new feature/config the user opts into; usually ref-guide documented
   - `changed` — improvement to existing behavior/perf, not opt-in
   - `fixed` — fixes buggy behavior (NPE, corruption, UI glitch)
   - `deprecated` — marks something deprecated
   - `removed` — code/feature removed
   - `dependency_update` — dependency version bump
   - `other` — refactorings, build/test-infra, docs

6. Delete every `#` comment line at the top of the file (the whole block down to the
   first real key). The result should start at `title:`.

7. Stage it:
   ```bash
   git add changelog/unreleased/<name>.yml
   ```

8. Show the final file contents to the user.
