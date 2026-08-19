# Bounded `format-fix` write path

UES v0.6 enables exactly one repository-mutating operation: `format-fix`.

## Command

The repository owner authorizes an exact PR candidate with a top-level PR comment:

```text
/exec format-fix sha=<40-char-head-sha> ref=<exact-head-ref> paths=<file1,file2> formatter=prettier-pinned
```

`paths` are exact existing files. Globs, symlinks, fork PRs, arbitrary shell commands, force pushes, merge, release, deploy, and unbounded writes are rejected.

## Trust split

1. `authorize` runs trusted UES code from the default branch. It derives `operation_id` from the GitHub comment ID, binds repository/ref/HEAD/tree/write-set, reads only trusted `github-actions[bot]` receipt markers, and publishes a durable PLANNED or REJECTED receipt.
2. `format` checks out the exact candidate with `persist-credentials: false`. It runs pinned Prettier in a job with `contents: read` only and produces a patch artifact. Candidate code/config never receives the repository write token.
3. `apply` is the only job with `contents: write`. It does not run project code. Trusted UES code validates the patch digest and changed paths, checks local and remote HEAD/tree, applies the patch with Git, commits, rechecks remote HEAD immediately before push, performs a normal non-force push, and confirms the resulting remote SHA.
4. `finalize` publishes a durable terminal receipt and preserves a small receipt artifact.

## Idempotency and recovery

`operation_id = github-comment:<comment-id>`.

A confirmed operation is not executed again. UNKNOWN is never blindly retried. The workflow is serialized per PR and the apply phase is additionally serialized per repository/ref.

For the bounded branch-only `format-fix` operation, a stale PLANNED/EXECUTING/UNKNOWN receipt may be terminalized only when live remote HEAD is still exactly its recorded start SHA. If HEAD moved, UES refuses automatic recovery and requires explicit reconciliation.

## Resource behavior

The formatter sandbox uses a small pinned standalone Prettier package rather than installing the target repository's full dependency tree. Patch/prepared artifacts are retained briefly; the final receipt artifact is retained longer for evidence. This keeps the first write path small while leaving room for future project-specific trusted formatter adapters.

## Activation

`issue_comment` workflows execute from the repository default branch. This write bridge is therefore inactive until the trusted UES bootstrap workflow is integrated into the default branch. The bootstrap PR can validate the implementation, but it cannot self-activate its own comment-triggered write workflow.
