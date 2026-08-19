# Dry-Run Mutation Transactions

UES v0.4 introduces the safety model for future mutating execution without enabling mutation itself.

## Current mode

All mutation planning is `DRY_RUN_ONLY`.

A successful plan returns:

- `decision = AUTHORIZED_DRY_RUN`;
- `eligible_for_future_execution = true`;
- `execution_enabled = false`;
- `safe_to_execute_now = false`.

No planner function edits files, creates commits, moves refs, opens pull requests, merges, deploys, releases, or changes secrets.

## Authority checks

A mutation plan validates the authority envelope against live state and the requested change set:

- repository;
- ref;
- exact HEAD SHA;
- optional exact tree SHA;
- operation name;
- allowed paths;
- prohibited paths, which always win;
- authorized resource classes;
- optional expiry;
- optional maximum changed-path count;
- optional operation/workstream binding when supplied by a trusted caller.

Path scopes are repository-relative and support segment-aware `*`, `?`, and recursive `**` matching. Path traversal and absolute paths fail closed.

## Leases

The planner can consume active lease records for the same repository/ref. A different operation conflicts when it overlaps a concrete requested path or resource class. Released and expired leases do not block. An invalid active lease is treated as a conflict rather than ignored.

The current core only calculates lease conflicts and a lease request. It does not persist or acquire a lease yet.

## CAS model

Every plan records the expected and live HEAD/tree state. A future executor must perform another live-state comparison immediately before the mutation. The planning decision is not permission to skip that comparison.

## Post-write reconciliation

`reconcile_post_write` is provided now so its behavior can be validated before mutation is enabled. It checks:

- whether the plan was authorized;
- whether observed changed paths stayed inside the planned write set;
- whether an expected post SHA/tree was reached;
- whether no write was observed;
- whether live state diverged and requires reconciliation.

It always returns `safe_to_blind_retry = false`.

## Trust boundary

The read-only `/exec mutation-plan` command may read authority/request/lease JSON from the candidate repository for planning and testing. Those candidate-contained files are **not trusted write authority**.

Before UES enables any write command, real authority must come from a trusted control-plane source outside untrusted candidate content and must be bound to the current operation, workstream, repository, ref, and exact live pre-state.

## Future activation gate

Mutation execution remains disabled until all of the following exist and pass independent tests:

1. trusted authority transport;
2. durable operation-id/idempotency registry;
3. durable lease acquisition/release;
4. immediate CAS check at the write boundary;
5. write-set verification after mutation;
6. UNKNOWN-write reconciliation before retry;
7. explicit policy for human approval gates on sensitive operations.
