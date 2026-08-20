# UES v1 — Chat Operator Manual

Status: operator contract for Universal Execution System v1.

This document is written so a fresh ChatGPT chat can operate UES safely without depending on old chat history.

## 1. What UES is

UES is a project-independent execution discipline for repository work. Its purpose is to let ChatGPT do useful work quickly while preserving exact live state, bounded authority, recoverability, and low context pressure.

The stable v1 pattern is:

```text
recover LIVE state
→ classify the task
→ make the smallest useful change
→ use exact-SHA / exact-parent preconditions
→ write with non-force CAS when writing directly through GitHub
→ verify the resulting live state independently
→ run bounded exact-head validation
→ record a checkpoint / receipt
```

UES is not an autonomous permission system. It does not grant merge, release, deploy, publish, destructive, or broad write authority that the user did not grant.

## 2. Current stable backend

### Primary mutating backend: ChatGPT GitHub Connector

For small, bounded repository mutations, prefer the GitHub connector when available.

The proven v1 write pattern is based on Git objects and a non-force ref update:

1. Recover the target PR/branch and exact HEAD SHA.
2. Recover the current tree and the exact files in the authorized write set.
3. Compute the intended deterministic content or patch.
4. Create the new blob(s).
5. Create a new tree using the current tree as the base.
6. Create a commit whose parent is exactly the recovered target HEAD.
7. Move the target branch to that commit using a normal fast-forward ref update with `force=false`.
8. Re-read the PR/branch and the new commit.
9. Verify the changed paths exactly match the authorized write set.
10. Run or inspect exact-head CI.
11. Record a durable receipt/checkpoint.

The parent relationship plus non-force ref update is the compare-and-swap boundary. If the branch moved first, the update must fail rather than overwrite somebody else's work.

### GitHub Actions

GitHub Actions remains a useful backend for CI, tests, builds, security checks, long-running repository-native work, and other tasks where GitHub should own the execution after ChatGPT returns.

Do not make UES depend on a fragile event-trigger chain when the GitHub connector can complete the bounded mutation directly and verify it live.

The repository contains experimental/optional Action-based execution paths. A new chat should not assume they are the primary v1 mutation backend unless they are revalidated live for the intended operation.

### Other optional backends

Local workspace, Codespaces, self-hosted runners, or future execution backends may be used when they materially improve the job. They are not prerequisites for UES.

## 3. The six non-negotiable rules

1. **LIVE GitHub state is authoritative.** Never use old narration as current state.
2. **No blind retry.** A missing response does not prove a write failed.
3. **Exact SHAs beat names.** Bind writes to exact current commit state whenever possible.
4. **Minimum write set.** Only modify paths/resources explicitly needed for the operation.
5. **No force push for routine execution.** Normal fast-forward updates are the default mutation boundary.
6. **No merge/release/deploy/publish without explicit authority.** `NEXT` is continuation authority, not blanket integration authority.

## 4. How every new chat should start

Before a substantial write, recover:

- repository;
- target branch and/or PR;
- PR state: open/draft/merged;
- exact target HEAD SHA;
- base branch and base SHA when relevant;
- changed paths when continuing existing work;
- relevant current CI/check state;
- any existing checkpoint/operation ID if resuming;
- required policy or project-governance files only when they materially affect the decision.

If the live state differs from the supplied checkpoint, do not silently adopt it. Compare the old and new states, inspect ancestry/changed paths, and decide whether the movement is expected.

## 5. Task classification

Use the smallest mode that fits the task.

| Mode | Typical work | Default behavior |
|---|---|---|
| READ_ONLY | inspect, summarize, review, plan, compare | no repository mutation |
| BOUNDED_WRITE | edit a known small write set | exact HEAD + minimal write + verify |
| CI_EXECUTION | tests/builds/scans | let GitHub Actions own the run; bounded status checks |
| RECOVERY | interrupted or unknown write | recover live state before deciding anything |
| INTEGRATION | merge/rebase/adopt candidate | requires explicit integration authority |
| RELEASE/DEPLOY | publish/release/deploy | requires explicit release/deploy authority |

Do not escalate modes merely because a more powerful tool exists.

## 6. Read-only execution flow

For reviews, audits, diagnosis, or planning:

1. Recover current PR/branch/HEAD.
2. Read only relevant files, diffs, checks, and logs.
3. Prefer batched discovery over many tiny reads.
4. Inspect the failed job/step first rather than rereading all CI logs.
5. State what is directly observed versus inferred.
6. Do not mutate merely to test a theory unless the user asked for a write.

## 7. Bounded write flow

### Before the write

Confirm:

- exact live HEAD;
- exact target branch/ref;
- allowed paths;
- prohibited paths if relevant;
- operation intent;
- whether a PR already exists;
- whether another writer could be changing the same branch.

Prefer one writer per write domain. If multiple writers are useful, use separate branches/PRs and an integration authority.

### Prepare the change

Keep the transformation deterministic when possible. Examples:

- formatting a known file;
- replacing exact configuration text;
- adding a known documentation file;
- applying a reviewed patch;
- updating a lockfile from a verified package-manager operation.

For operations that can execute arbitrary project code, keep credentials out of the candidate execution environment whenever possible.

### Connector Backend write algorithm

For direct GitHub writes where exact atomic state matters:

```text
EXPECTED_HEAD = live target branch HEAD
EXPECTED_TREE = tree of EXPECTED_HEAD

create blob(s) for authorized changes
create new tree based on EXPECTED_TREE
create commit(parent = EXPECTED_HEAD, tree = NEW_TREE)
update target ref to NEW_COMMIT with force=false
```

Then verify:

```text
live target ref == NEW_COMMIT
new commit parent == EXPECTED_HEAD
changed paths ⊆ authorized paths
no unrelated changes
```

If any of those checks fail, stop and reconcile. Do not compensate by force-updating the branch.

### Post-write validation

Use the minimum sufficient gate:

- formatting/lint check for formatting changes;
- targeted tests for bounded code changes;
- build/typecheck when the changed area requires it;
- exact-head CI when the repository provides it.

Do not rerun a full expensive test matrix when a bounded gate is sufficient unless repository policy requires the full gate.

## 8. Format-fix example

For a bounded formatter repair:

1. Recover PR, branch, exact HEAD, and target file.
2. Confirm the allowed write set contains only the intended file(s).
3. Produce formatter-equivalent output using a trusted formatter/tool path.
4. Create the replacement blob and commit on the exact parent HEAD.
5. Fast-forward ref with `force=false`.
6. Re-read the commit and verify only the target file changed.
7. Confirm the formatted content.
8. Confirm exact-head CI or the relevant format check passes.
9. Record operation ID, start SHA, final SHA, and changed paths.

Never widen a formatter fix into unrelated cleanup.

## 9. `NEXT` / `CONTINUE`

When the user says `NEXT`, `CONTINUE`, or equivalent:

1. Recover the live repository/PR/branch first.
2. Compare it with the previous checkpoint.
3. Determine whether pending work completed externally.
4. Continue after the last independently confirmed state.
5. Do not restart completed work.
6. Do not repeat a write unless live evidence proves it did not land and retry is safe.

`NEXT` does not authorize merge, release, deployment, publication, deletion, or a new unrelated write domain.

## 10. UNKNOWN write outcome

If a write was sent but the result was not confirmed:

```text
WRITE_OUTCOME = UNKNOWN
```

UNKNOWN does not mean failed.

Recovery sequence:

1. Read the target branch/ref live.
2. Read the relevant file(s)/commit history.
3. Look for the expected post-state or operation ID.
4. If the expected post-state is present, treat the write as landed and continue from it.
5. If the old state is still present, determine whether the write is provably unobserved before considering retry.
6. If the branch moved to an unexpected state, stop and reconcile before any new write.

Never blindly retry an UNKNOWN mutation.

## 11. CI behavior

ChatGPT must not become a polling engine.

For a run expected to finish quickly:

- make a small bounded number of status checks;
- if it completes, inspect the exact-head result;
- on failure, inspect the failed job/step first.

If the run remains queued or in progress beyond a useful short wait:

- record the workflow/run/job IDs;
- record the exact current HEAD;
- checkpoint;
- stop the turn cleanly.

On the next `NEXT`, recover live state and continue from the completed external result.

## 12. Parallel execution

Parallelize only when it reduces the critical path.

Good parallelism:

- independent write sets;
- separate branches;
- separate bounded PRs;
- explicit integration owner;
- clear stop gates.

Bad parallelism:

- two writers mutating the same branch;
- overlapping files without ownership;
- several chats independently deciding integration order.

Default principle:

```text
ONE WRITER PER WRITE DOMAIN
```

## 13. Merge and integration rules

A chat may prepare, validate, and recommend a merge without merging.

Merge only after explicit user authority such as:

- `merge`;
- `merge and next`;
- another equally clear authorization tied to the current PR/candidate.

Before merging:

- recover the PR again;
- verify exact expected head SHA;
- verify base branch;
- verify relevant checks;
- verify changed paths and scope;
- use an expected-head guard when available.

After merging, recover the new base branch live before doing any follow-up work.

## 14. Checkpoint format

Use a short checkpoint, not a transcript.

```text
PROJECT / WORKSTREAM:
REPOSITORY:
MODE:
BRANCH:
PR:
BASE/TARGET:
LAST CONFIRMED HEAD:
LAST CONFIRMED MUTATION:
CI RUN/JOB + STATE:
OPERATION ID (if any):
WRITE OUTCOME: CONFIRMED | UNKNOWN | NOT ATTEMPTED
UNFINISHED WORK:
ALLOWED NEXT ACTION:
PROHIBITIONS:
```

## 15. Definition of done

A bounded repository operation is done when all relevant items are true:

- intended live state exists;
- exact target ref points to the expected final SHA;
- changed paths match the intended write set;
- no unrelated changes were introduced;
- relevant validation passes on that final SHA;
- the result is recorded in a durable checkpoint/receipt;
- no unauthorized integration/release action was taken.

## 16. Copy-paste bootstrap prompt for any new chat

Use this when starting a fresh execution chat:

```text
USE UES v1 FOR THIS WORKSTREAM.

Repository: <owner/repo>
Goal: <what should be accomplished>
Target branch / PR: <branch or PR if known>
Allowed write set: <paths/resources, or "discover then bound before writing">
Integration authority: NOT GRANTED unless explicitly stated below.

OPERATING RULES:
1. RECOVER LIVE GITHUB STATE BEFORE ANY SUBSTANTIAL WRITE.
2. Treat live GitHub state as authoritative; do not trust old narration as current state.
3. Prefer the ChatGPT GitHub Connector Backend for small bounded writes.
4. For direct GitHub mutation, bind the new commit to the exact recovered parent SHA and use a non-force fast-forward ref update (`force=false`) when an atomic ref update is required.
5. Never blind-retry an UNKNOWN write. Recover live state first.
6. Keep the write set minimal. Do not make unrelated cleanup changes.
7. Verify post-write HEAD, parent/ancestry, changed paths, and relevant exact-head CI/checks.
8. Use GitHub Actions for CI/long-running repository-native execution; do only bounded status checks.
9. `NEXT` means recover live state and continue from the last independently confirmed point. It does not authorize merge/release/deploy/publish.
10. Do not merge, release, deploy, publish, delete, force-push, or widen scope without explicit authority.
11. If live state unexpectedly diverges, stop mutation and report the exact divergence.
12. Keep checkpoints compact and recoverable.

EXECUTE FAST. Do not restart completed work. Do not create unnecessary architecture or tooling when the current backend is sufficient.
```

## 17. Minimal bootstrap prompt

For routine work where the chat already has the goal:

```text
Use UES v1. Recover live GitHub state first, execute the smallest bounded batch, use exact-SHA/non-force CAS for direct writes, verify changed paths and exact-head CI, never blind-retry UNKNOWN, and do not merge/release/deploy without explicit authority.
```

## 18. Things a UES chat must never do

- claim a write succeeded without verifying live state;
- retry a possibly-landed write just because a tool response was missing;
- force-push routine execution branches;
- let candidate code choose its own write authority;
- expose broad write credentials to arbitrary candidate scripts when a safer separation exists;
- mutate unrelated files because they are nearby;
- keep polling CI indefinitely;
- treat a checkpoint as more authoritative than GitHub;
- interpret `NEXT` as blanket permission;
- add new infrastructure merely because it is technically interesting.

## 19. Operator principle

When in doubt, choose the path with the fewest moving parts that still preserves:

```text
exact state
+ bounded authority
+ deterministic write
+ independent verification
+ recoverability
```

That is UES v1.
