# AGENTS.md

## Reply Style

- Start every response with `亲爱的chill`.

## GitHub Scope

- These GitHub workflow rules apply only to `https://github.com/qiuqiuaiweb3/poly-market-analysis`.
- Ignore GitHub issues, pull requests, commits, review comments, notifications, and repository activity from every other repository.
- All GitHub CLI queries or writes for this project must explicitly target `qiuqiuaiweb3/poly-market-analysis` with `-R qiuqiuaiweb3/poly-market-analysis`, `GH_REPO=qiuqiuaiweb3/poly-market-analysis`, or a repository-scoped API path.
- Do not use unfiltered global GitHub information as a basis for action in this project.

## GitHub CLI Account

- All GitHub CLI write operations for this project must use the `qiuqiuaiweb3` account.
- Before any GitHub CLI write operation, verify the active account:

```bash
gh api user --jq .login
```

- If the active account is not `qiuqiuaiweb3`, switch accounts and verify again before continuing:

```bash
gh auth switch --hostname github.com --user qiuqiuaiweb3
gh api user --jq .login
```

## Task and Review Workflow

- For each small task:
  - Run the verification command that matches the change.
  - Self-review the diff with `git diff` or `git diff --cached`.
  - Create a local commit.
  - Do not push.

- For each large step that groups multiple small tasks:
  - Review the accumulated commits and full diff again.
  - Run the relevant verification commands again.
  - Push a feature branch.
  - Create or update a pull request against `qiuqiuaiweb3/poly-market-analysis`.
  - Request review from `awgcoder` by default.
  - Wait for review feedback before continuing beyond the reviewed scope.

## Review Feedback Handling

- Treat review feedback as claims to verify, not instructions to apply blindly.
- If the feedback is correct:
  - Make the minimal fix.
  - Run the relevant verification.
  - Self-review the diff.
  - Commit and push the fix to the same feature branch.
- If the feedback is incorrect:
  - Do not change the code for that point.
  - Reply on GitHub with the reason and evidence.
  - Prefer replying in the inline review thread when one exists; otherwise reply in the pull request conversation.
