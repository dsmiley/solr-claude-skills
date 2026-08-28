---
name: branch-commit-msg
description: Summarize the current branch's changes vs main into a commit message and copy it to the clipboard. Use when user wants a commit message for the whole branch.
user_invocable: true
argument-hint: [base-branch]
---

# branch-commit-msg

Generates a commit message summarizing all commits on the current branch since it diverged from the base branch.

## Steps

1. Determine the base branch: use the argument if provided, otherwise default to `main`.

2. Find the merge base and list substantive commits:
```bash
git log --oneline <base-branch>...HEAD --no-merges
```

3. Get all files modified in the branch:
```bash
git diff --name-only <base-branch>...HEAD
```

4. For any non-.java files that were modified, read the full diff:
```bash
git diff <base-branch>...HEAD -- <file>
```

5. Review commit messages, file names, and non-Java diffs. Ignore meta/housekeeping commits (e.g. "AGENTS:", "feedback:", "changelog", "Initial plan").

6. Synthesize a commit message that captures the *what and why* of the substantive changes. Do not list every commit — write a unified summary.  Newline after every sentence/bullet.

7. Copy the message to the clipboard with a here-doc, then show it to user:
```bash
cat << 'EOF' | pbcopy
<synthesized message>
EOF
echo "✓ Commit message copied to clipboard"
cat << 'EOF'
<synthesized message>
EOF
```