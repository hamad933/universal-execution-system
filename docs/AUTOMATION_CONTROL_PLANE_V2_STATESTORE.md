# UES Automation Control Plane V2 — Production StateStore Backend

Status: `IMPLEMENTATION CANDIDATE / NOT ACTIVATED / NO LIVE STATE REFS`

## Decision

The production cross-run StateStore candidate uses the GitHub Git Data API against a
**dedicated private runtime-state repository**. It does not use the public UES source
repository for runtime state.

The backend shards durable state into two ref classes:

- one ref per canonical `lane_id` for the workstream runtime record and lane-local lease;
- one ref per canonical operation key for durable operation / idempotency state.

This preserves the existing R2 `StateStore` interface without changing frozen Domain D.
`read_operation(operation_key)` does not receive `lane_id`, so operation state cannot be
located safely inside only a lane ref without either a global index or a Domain-D API
change. Operation refs avoid both.

Ref names expose only SHA-256 digests of identities. The canonical lane/operation identity
remains inside the sanitized JSON snapshot and is revalidated on every read.

## CAS and concurrency

A write follows:

1. read the authoritative ref;
2. read and validate the exact snapshot/version;
3. create immutable Git blob/tree/commit objects whose parent is the exact ref SHA read;
4. update the ref with `force=false`;
5. read the authoritative ref again.

A non-fast-forward or create-ref collision fails closed. An ambiguous network/429/5xx
write is never blindly repeated. Ref readback determines:

- ref == proposed commit: the state mutation is confirmed;
- ref == previous commit: the mutation was not observed and fails closed;
- any other ref: divergent/unknown state requiring reconciliation.

Independent lanes use different refs, so one blocked or concurrently updated lane does not
create a portfolio-wide lock.

## Persistence model

Each ref commit contains exactly `state.json` with:

- UES runtime schema version;
- backend schema `ues-github-ref-state-v1`;
- record kind (`lane` or `operation`);
- full identity;
- monotonic per-record version;
- sanitized runtime record.

The backend applies `sanitize_receipt()` to the entire persisted snapshot. API tokens and
secrets are runtime-only and never appear in state JSON, ref names, commit messages, or
exception text.

## Storage authority and privacy

The runtime-state repository must be private. `GitHubGitDataTransport` checks repository
privacy when constructed and rejects a public repository by default.

A real deployment still requires a separately governed private state repository and a
narrow runtime credential that can access only that state repository. This implementation
does not create that repository, create live runtime refs, authorize canary, or grant any
project/provider mutation authority.

## Activation boundary

The backend implements storage mechanics only. A technically production-capable
StateStore does not itself authorize:

- CANARY;
- ACTIVE_AUTO_SAFE;
- provider mutation;
- new task/session creation;
- GitHub product writes;
- merge/release/deploy.

Those remain project/Parent authority decisions and must pass the existing action-policy,
effect-identity, evidence, canary-grant and provider safety layers.

## Validation

Repository tests use a deterministic in-memory Git-ref transport. They cover:

- cross-run restoration;
- CAS conflicts without overwrite;
- lane isolation for same `W01` in different projects;
- operation durability despite non-reversible operation keys;
- lane-local durable leases;
- secret redaction before persistence;
- ambiguous write applied/not-applied/diverged outcomes;
- initialization races;
- no global ref contention across independent lanes.

No test creates or mutates a live GitHub runtime-state ref.
